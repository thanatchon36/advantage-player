"""Thin async HMAC-SHA256 signed REST client for Binance USDⓈ-M futures.

Retry policy (per Binance general-info docs):
- 429            → back off (Retry-After) and retry.
- 418            → IP banned; raise, never retry.
- -1021          → timestamp out of recvWindow; resync clock and retry once.
- 503 "Unknown error" / read timeout on a mutating call → status UNKNOWN: raise
  UnknownStatusError so the caller queries by client id instead of re-sending.
- other 5xx, -1008 throttle, connect errors → exponential backoff and retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from advantage_player.exchange.clock import Clock
from advantage_player.exchange.ratelimit import RateLimiter

log = structlog.get_logger(__name__)

Params = dict[str, Any]
CLIENT_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


class BinanceError(Exception):
    pass


class BinanceAPIError(BinanceError):
    def __init__(self, status: int, code: int, msg: str) -> None:
        super().__init__(f"HTTP {status} code={code}: {msg}")
        self.status = status
        self.code = code
        self.msg = msg


class IPBannedError(BinanceError):
    pass


class UnknownStatusError(BinanceError):
    """The request may or may not have executed; caller must query before retrying."""


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def new_client_id(tag: str) -> str:
    """Idempotency key valid for newClientOrderId / clientAlgoId: ^[.A-Z:/a-z0-9_-]{1,36}$."""
    tag = "".join(c for c in tag if c.isalnum() or c in "_-")[:12]
    rand = "".join(secrets.choice(CLIENT_ID_CHARS) for _ in range(16))
    return f"ap_{tag}_{rand}"[:36]


class BinanceRest:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        *,
        clock: Clock | None = None,
        limiter: RateLimiter | None = None,
        recv_window_ms: int = 5000,
        max_retries: int = 4,
        backoff_s: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._secret = api_secret
        self.clock = clock or Clock()
        self.limiter = limiter or RateLimiter()
        self.recv_window_ms = recv_window_ms
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def sync_time(self) -> None:
        async def fetch() -> float:
            data = await self.public("GET", "/fapi/v1/time")
            return float(data["serverTime"])

        await self.clock.sync(fetch)

    async def public(self, method: str, path: str, params: Params | None = None) -> Any:
        return await self._request(method, path, params or {}, signed=False)

    async def signed(self, method: str, path: str, params: Params | None = None) -> Any:
        return await self._request(method, path, params or {}, signed=True)

    async def api_key_only(self, method: str, path: str, params: Params | None = None) -> Any:
        """USER_STREAM endpoints (listenKey): API key header, no signature."""
        return await self._request(method, path, params or {}, signed=False, key=True)

    def _build_query(self, params: Params, signed: bool) -> str:
        items = [(k, _fmt(v)) for k, v in params.items() if v is not None]
        if signed:
            items.append(("timestamp", str(self.clock.now_ms())))
            items.append(("recvWindow", str(self.recv_window_ms)))
            q = urlencode(items)
            return f"{q}&signature={sign(self._secret, q)}"
        return urlencode(items)

    async def _request(
        self, method: str, path: str, params: Params, *, signed: bool, key: bool = False
    ) -> Any:
        mutating = method.upper() != "GET"
        resynced = False
        attempt = 0
        while True:
            query = self._build_query(params, signed)
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            headers = {"X-MBX-APIKEY": self._key} if (signed or key) else {}
            try:
                resp = await self._http.request(method, url, headers=headers)
            except httpx.ConnectError as e:  # never reached the server → safe to retry
                if attempt >= self.max_retries:
                    raise BinanceError(f"connect failed: {e}") from e
            except httpx.TimeoutException as e:
                if mutating:
                    raise UnknownStatusError(f"{method} {path} timed out: {e}") from e
                if attempt >= self.max_retries:
                    raise BinanceError(f"timeout: {e}") from e
            else:
                self.limiter.update(resp.headers)
                if resp.status_code < 400:
                    return resp.json()
                retry = self._classify(resp, mutating)
                if retry == "resync" and not resynced:
                    resynced = True
                    await self.sync_time()
                    continue
                if retry != "retry" or attempt >= self.max_retries:
                    self._raise(resp)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", self.backoff_s * 2**attempt))
                    log.warning("rate_limited", path=path, wait_s=wait)
                    await asyncio.sleep(wait)
                    attempt += 1
                    continue
            await asyncio.sleep(self.backoff_s * 2**attempt)
            attempt += 1

    @staticmethod
    def _payload(resp: httpx.Response) -> tuple[int, str]:
        try:
            body = resp.json()
            return int(body.get("code", 0)), str(body.get("msg", resp.text))
        except (ValueError, AttributeError):
            return 0, resp.text

    def _classify(self, resp: httpx.Response, mutating: bool) -> str:
        code, msg = self._payload(resp)
        s = resp.status_code
        if s == 418:
            raise IPBannedError(msg)
        if code == -1021:
            return "resync"
        if s == 429 or code == -1008:
            return "retry"
        if s >= 500:
            m = msg.lower()
            known_failure = "service unavailable" in m or "internal error" in m
            if mutating and not known_failure:
                raise UnknownStatusError(f"HTTP {s}: {msg}")
            return "retry"
        return "raise"

    def _raise(self, resp: httpx.Response) -> None:
        code, msg = self._payload(resp)
        raise BinanceAPIError(resp.status_code, code, msg)
