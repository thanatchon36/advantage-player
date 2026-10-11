"""User data stream: source of truth for orders, algo orders and account (SPEC §8).

URL (docs + verified on DEMO): <ws_base>/private/ws/<listenKey>. listenKey lives 60 min after
its last keepalive → PUT every 30 min; `listenKeyExpired` or -1125 → create a new key.
Connections are rotated before the 24 h forced disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
import websockets

from advantage_player.exchange.rest import BinanceAPIError, BinanceRest

log = structlog.get_logger(__name__)
Handler = Callable[[dict[str, Any]], Awaitable[None]]


class UserStream:
    def __init__(
        self,
        rest: BinanceRest,
        ws_base: str,
        handler: Handler,
        keepalive_s: float = 1800,
        rotate_after_s: float = 23 * 3600,
    ) -> None:
        self.rest = rest
        self.ws_base = ws_base
        self.handler = handler
        self.keepalive_s = keepalive_s
        self.rotate_after_s = rotate_after_s
        self.listen_key: str | None = None
        self.connected = False
        self.last_event_ms = 0
        self._tasks: list[asyncio.Task[None]] = []
        self.on_reconnect: Callable[[], Awaitable[None]] | None = None

    async def _new_key(self) -> str:
        r = await self.rest.api_key_only("POST", "/fapi/v1/listenKey")
        self.listen_key = str(r["listenKey"])
        return self.listen_key

    async def start(self) -> None:
        await self._new_key()
        self._tasks = [
            asyncio.create_task(self._run(), name="uds"),
            asyncio.create_task(self._keepalive(), name="uds-keepalive"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self.connected = False

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive_s)
            try:
                await self.rest.api_key_only("PUT", "/fapi/v1/listenKey")
            except BinanceAPIError as e:
                log.warning("listenkey_keepalive_failed", code=e.code)
                await self._new_key()

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while True:
            url = f"{self.ws_base}/private/ws/{self.listen_key}"
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=60) as ws:
                    self.connected = True
                    backoff = 1.0
                    log.info("uds_connected")
                    if not first and self.on_reconnect:
                        await self.on_reconnect()  # resync state via REST after a gap
                    first = False
                    opened = time.monotonic()
                    while time.monotonic() - opened < self.rotate_after_s:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except TimeoutError:
                            continue
                        msg = json.loads(raw)
                        self.last_event_ms = int(time.time() * 1000)
                        if msg.get("e") == "listenKeyExpired":
                            log.warning("listenkey_expired")
                            await self._new_key()
                            break
                        await self.handler(msg)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                self.connected = False
                log.warning("uds_error", error=repr(e), backoff_s=backoff)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)
            self.connected = False
