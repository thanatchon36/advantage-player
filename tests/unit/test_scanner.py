from __future__ import annotations

import httpx
import numpy as np
import pytest
import respx

from advantage_player.config.settings import AppConfig, UniverseCfg, load_config
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.models import Fees, SymbolInfo
from advantage_player.exchange.rest import BinanceRest
from advantage_player.marketdata.state import MarketState
from advantage_player.scanner.metrics import (
    TableMetrics,
    atr,
    correlation_clusters,
    depth_within_bps,
    ineligibility,
    wick_anomaly,
)
from advantage_player.scanner.scanner import TableScanner, universe

DAY = 86_400_000
NOW = 1_791_700_000_000


def test_atr_constant_range() -> None:
    n = 30
    assert atr([11.0] * n, [9.0] * n, [10.0] * n) == pytest.approx(2.0)
    assert np.isnan(atr([1.0], [1.0], [1.0]))


def test_depth_within_bps() -> None:
    bids = [(100.0, 1.0), (99.95, 2.0), (99.0, 50.0)]
    asks = [(100.05, 1.0), (100.2, 9.0)]
    b, a = depth_within_bps(bids, asks, 10)  # mid 100.025 → [99.925, 100.125]
    assert b == pytest.approx(100 + 199.9)
    assert a == pytest.approx(100.05)


def test_wick_anomaly() -> None:
    h = [1.01] * 99
    lo = [1.0] * 99
    assert not wick_anomaly(h, lo)
    h[-5] = 1.2
    assert wick_anomaly(h, lo)


def test_clusters() -> None:
    rng = np.random.default_rng(0)
    base = rng.normal(size=200)
    rets = {
        "BTC": base,
        "ETH": base + rng.normal(scale=0.2, size=200),
        "ALT": rng.normal(size=200),
    }
    cl = correlation_clusters(rets, 0.7)
    assert {"BTC", "ETH"} in cl and {"ALT"} in cl


def _m(**kw: object) -> TableMetrics:
    base: dict[str, object] = dict(
        symbol="X",
        price=100,
        quote_volume=60e6,
        depth_bid_usd=3e5,
        depth_ask_usd=3e5,
        spread_bps=1.0,
        tick_bps=1.0,
        maker_fee_bps=2,
        taker_fee_bps=4,
        slippage_bps=1,
        funding_rate=0.0001,
        atr_bps={"1m": 60.0, "5m": 120.0},
    )
    base.update(kw)
    return TableMetrics(**base)  # type: ignore[arg-type]


def test_cost_and_move_to_cost() -> None:
    m = _m()
    assert m.round_trip_cost_bps("taker") == 2 * 4 + 1 + 1
    assert m.round_trip_cost_bps("maker") == 2 * 2 + 1 + 1
    assert m.move_to_cost("1m", "taker") == pytest.approx(6.0)
    assert m.score([("1m", "taker"), ("5m", "taker")]) == pytest.approx(12.0)
    assert m.move_to_cost("15s", "taker") == 0.0  # unknown TF never qualifies


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"quote_volume": 49e6}, "quote_volume"),
        ({"depth_ask_usd": 2e5}, "depth"),
        ({"tick_bps": 2.5}, "tick_bps"),
        ({"funding_rate": -0.0006}, "funding"),
        ({"anomaly": True}, "anomaly"),
    ],
)
def test_ineligibility_each_rule(kw: dict[str, object], reason: str) -> None:
    cfg = UniverseCfg()
    assert ineligibility(_m(), cfg, 6.0) == []
    assert ineligibility(_m(**kw), cfg, 6.0) == [reason]
    assert ineligibility(_m(), cfg, 4.9) == ["move_to_cost"]


def _sym(
    name: str,
    onboard: int,
    status: str = "TRADING",
    delivery: int = 4133404802000,
    ctype: str = "PERPETUAL",
) -> SymbolInfo:
    from decimal import Decimal as D

    return SymbolInfo(
        name,
        status,
        ctype,
        "USDT",
        onboard,
        delivery,
        D("0.01"),
        D("0.001"),
        D("0.001"),
        D("1000"),
        D("0.001"),
        D("100"),
        D("5"),
    )


def test_universe_filters() -> None:
    meta = ExchangeMetadata(BinanceRest("https://x"))
    meta.symbols = {
        s.symbol: s
        for s in [
            _sym("OKUSDT", NOW - 40 * DAY),
            _sym("NEWUSDT", NOW - 10 * DAY),
            _sym("HALTUSDT", NOW - 40 * DAY, status="SETTLING"),
            _sym("DELISTUSDT", NOW - 40 * DAY, delivery=NOW + 5 * DAY),
            _sym("QTRUSDT", NOW - 40 * DAY, ctype="CURRENT_QUARTER"),
        ]
    }
    assert [s.symbol for s in universe(meta, AppConfig(), NOW)] == ["OKUSDT"]


def _klines(n: int, px: float, rng: float, step: int = 60_000) -> list[list[object]]:
    return [[i * step, px, px + rng / 2, px - rng / 2, px, 1, 0, 1, 1, 1, 1, 0] for i in range(n)]


@respx.mock
async def test_scan_ranks_and_tracks_changes() -> None:
    base = "https://x"
    cfg = load_config("config.yaml")
    meta = ExchangeMetadata(BinanceRest(base))
    meta.symbols = {s: _sym(s, NOW - 100 * DAY) for s in ["AUSDT", "BUSDT", "CUSDT"]}
    meta.fees = {s: Fees(s, 0.0002, 0.0004) for s in meta.symbols}
    respx.get(f"{base}/fapi/v1/ticker/24hr").respond(
        200,
        json=[
            {"symbol": "AUSDT", "quoteVolume": "9e8"},
            {"symbol": "BUSDT", "quoteVolume": "9e8"},
            {"symbol": "CUSDT", "quoteVolume": "1e6"},  # fails volume prefilter
        ],
    )
    respx.get(f"{base}/fapi/v1/premiumIndex").respond(
        200, json=[{"symbol": s, "lastFundingRate": "0.0001"} for s in meta.symbols]
    )
    book = {"bids": [["99.99", "5000"]], "asks": [["100.01", "5000"]]}
    respx.get(f"{base}/fapi/v1/depth").respond(200, json=book)
    respx.get(f"{base}/fapi/v1/openInterest").respond(200, json={"openInterest": "10"})

    def klines(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        rng = 1.0 if p["symbol"] == "AUSDT" else 0.2  # A is far more volatile
        return httpx.Response(200, json=_klines(int(p["limit"]), 100.0, rng))

    respx.get(f"{base}/fapi/v1/klines").mock(side_effect=klines)
    sc = TableScanner(BinanceRest(base), meta, MarketState(), cfg)
    res = await sc.scan(NOW)
    assert set(res.metrics) == {"AUSDT", "BUSDT"}
    assert res.open == ["AUSDT"]  # B fails move_to_cost
    assert "move_to_cost" in res.reasons["BUSDT"]
    assert res.watch == ["BUSDT"]
    assert res.added == ["AUSDT"] and res.removed == []
    res2 = await sc.scan(NOW + 1)
    assert res2.added == [] and res2.metrics["AUSDT"].oi_change == 0.0
