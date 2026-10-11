from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from advantage_player.exchange.clock import Clock
from advantage_player.exchange.rest import (
    BinanceAPIError,
    BinanceRest,
    IPBannedError,
    UnknownStatusError,
    new_client_id,
    sign,
)

BASE = "https://demo-fapi.binance.com"


def test_signature_matches_binance_docs_example() -> None:
    # Example from Binance USDⓈ-M "SIGNED Endpoint Examples".
    secret = "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9"
    q = (
        "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000"
        "&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
    )
    assert sign(secret, q) == "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"


def test_client_id_format() -> None:
    pat = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")
    ids = {new_client_id("A_trend/1m BTC!!") for _ in range(200)}
    assert len(ids) == 200
    assert all(pat.match(i) for i in ids)


def _client(**kw: object) -> BinanceRest:
    clock = Clock(local_ms=lambda: 1_000_000.0)
    return BinanceRest(BASE, "key", "secret", clock=clock, backoff_s=0.0, **kw)  # type: ignore[arg-type]


@respx.mock
async def test_signed_request_has_key_timestamp_signature_last() -> None:
    route = respx.get(f"{BASE}/fapi/v1/leverageBracket").respond(200, json=[])
    c = _client()
    await c.signed("GET", "/fapi/v1/leverageBracket", {"symbol": "BTCUSDT"})
    req = route.calls.last.request
    assert req.headers["X-MBX-APIKEY"] == "key"
    q = urlsplit(str(req.url)).query
    pairs = parse_qsl(q)
    assert [k for k, _ in pairs] == ["symbol", "timestamp", "recvWindow", "signature"]
    unsigned = q.rsplit("&signature=", 1)[0]
    assert dict(pairs)["signature"] == sign("secret", unsigned)
    assert dict(pairs)["timestamp"] == "1000000"


@respx.mock
async def test_weight_headers_tracked() -> None:
    respx.get(f"{BASE}/fapi/v1/time").respond(
        200, json={"serverTime": 1}, headers={"X-MBX-USED-WEIGHT-1M": "1200"}
    )
    c = _client()
    await c.public("GET", "/fapi/v1/time")
    assert c.limiter.used[("REQUEST_WEIGHT", "1MINUTE")] == 1200
    assert c.limiter.headroom() == pytest.approx(0.5)


@respx.mock
async def test_429_retries_then_succeeds() -> None:
    route = respx.get(f"{BASE}/fapi/v1/time")
    route.side_effect = [
        httpx.Response(429, json={"code": -1003, "msg": "too many"}, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"serverTime": 5}),
    ]
    assert (await _client().public("GET", "/fapi/v1/time"))["serverTime"] == 5
    assert route.call_count == 2


@respx.mock
async def test_418_raises_banned_without_retry() -> None:
    route = respx.get(f"{BASE}/fapi/v1/time").respond(418, json={"code": -1003, "msg": "ban"})
    with pytest.raises(IPBannedError):
        await _client().public("GET", "/fapi/v1/time")
    assert route.call_count == 1


@respx.mock
async def test_503_unknown_on_order_post_is_not_retried() -> None:
    route = respx.post(f"{BASE}/fapi/v1/order").respond(
        503, json={"code": -1000, "msg": "Unknown error, please check your request"}
    )
    with pytest.raises(UnknownStatusError):
        await _client().signed("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert route.call_count == 1


@respx.mock
async def test_timeout_on_post_is_unknown_status() -> None:
    respx.post(f"{BASE}/fapi/v1/algoOrder").side_effect = httpx.ReadTimeout("slow")
    with pytest.raises(UnknownStatusError):
        await _client().signed("POST", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT"})


@respx.mock
async def test_service_unavailable_get_retries() -> None:
    route = respx.get(f"{BASE}/fapi/v1/time")
    route.side_effect = [
        httpx.Response(503, json={"code": -1000, "msg": "Service Unavailable."}),
        httpx.Response(200, json={"serverTime": 7}),
    ]
    assert (await _client().public("GET", "/fapi/v1/time"))["serverTime"] == 7


@respx.mock
async def test_minus_1021_resyncs_clock_and_retries_once() -> None:
    respx.get(f"{BASE}/fapi/v1/time").respond(200, json={"serverTime": 1_005_000})
    route = respx.get(f"{BASE}/fapi/v1/account")
    route.side_effect = [
        httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"}),
        httpx.Response(200, json={"ok": True}),
    ]
    c = _client()
    assert await c.signed("GET", "/fapi/v1/account") == {"ok": True}
    assert c.clock.offset_ms == pytest.approx(5000)


@respx.mock
async def test_api_error_raised_with_code() -> None:
    respx.post(f"{BASE}/fapi/v1/order").respond(
        400, json={"code": -4120, "msg": "Order type not supported for this endpoint."}
    )
    with pytest.raises(BinanceAPIError) as ei:
        await _client().signed("POST", "/fapi/v1/order", {"type": "STOP_MARKET"})
    assert ei.value.code == -4120
