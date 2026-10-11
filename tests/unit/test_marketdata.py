from __future__ import annotations

from pathlib import Path

import pytest

from advantage_player.marketdata.bars import Bar, BarBuilder, tf_seconds
from advantage_player.marketdata.events import (
    AggTrade,
    BookTicker,
    Depth,
    MarkPrice,
    parse_event,
)
from advantage_player.marketdata.hub import MarketDataHub
from advantage_player.marketdata.recorder import TickRecorder, load_ticks
from advantage_player.marketdata.state import MarketState
from advantage_player.marketdata.streams import route_for, shard, symbol_streams

# payloads captured from DEMO streams on 2026-10-11
AGG = {
    "e": "aggTrade",
    "E": 1791726019091,
    "a": 314464427,
    "s": "BTCUSDT",
    "p": "83059.00",
    "q": "0.0010",
    "nq": "0.0010",
    "f": 545858278,
    "l": 545858278,
    "T": 1791726018934,
    "m": False,
    "st": 1,
}
BOOK = {
    "e": "bookTicker",
    "u": 450007988101,
    "s": "BTCUSDT",
    "ps": "BTCUSDT",
    "b": "83047.30",
    "B": "177.4741",
    "a": "83059.00",
    "A": "7.8264",
    "T": 1791726018934,
    "E": 1791726018934,
}
DEPTH = {
    "e": "depthUpdate",
    "E": 1791726018945,
    "T": 1791726018934,
    "s": "BTCUSDT",
    "U": 1,
    "u": 2,
    "pu": 0,
    "b": [["100", "3"], ["99", "1"]],
    "a": [["101", "1"]],
}
MARK = {
    "e": "markPriceUpdate",
    "E": 1791726019000,
    "s": "BTCUSDT",
    "p": "83059.0",
    "ap": "83059.0",
    "P": "83101.0",
    "i": "83115.1",
    "r": "0.00010000",
    "T": 1791734400000,
}


def test_parse_events() -> None:
    a = parse_event(AGG)
    assert isinstance(a, AggTrade) and a.signed_qty == pytest.approx(0.001)
    b = parse_event(BOOK)
    assert isinstance(b, BookTicker) and b.spread_bps == pytest.approx(1.4087, rel=1e-3)
    d = parse_event(DEPTH)
    assert isinstance(d, Depth) and d.imbalance() == pytest.approx(0.8)
    m = parse_event(MARK)
    assert isinstance(m, MarkPrice) and m.funding_rate == pytest.approx(0.0001)
    assert parse_event({"e": "kline"}) is None


def test_routing_follows_docs() -> None:
    assert route_for("btcusdt@aggTrade") == "market"
    assert route_for("btcusdt@markPrice@1s") == "market"
    assert route_for("btcusdt@bookTicker") == "public"
    assert route_for("btcusdt@depth5@100ms") == "public"


def test_shard_respects_limit_and_route() -> None:
    streams = [s for i in range(30) for s in symbol_streams(f"S{i}USDT", True)]
    shards = shard(streams, max_per_conn=25)
    assert all(len(c) <= 25 for _, c in shards)
    assert all(route_for(s) == r for r, c in shards for s in c)
    assert sum(len(c) for _, c in shards) == 120
    assert len(symbol_streams("X", False)) == 2


def _t(ms: int, px: float, q: float = 1.0, maker: bool = False, aid: int = 0) -> AggTrade:
    return AggTrade("X", aid or ms, px, q, ms, maker)


def test_bar_builder_ohlcv_delta_and_gaps() -> None:
    assert tf_seconds("15s") == 15 and tf_seconds("5m") == 300
    bb = BarBuilder("X", "5s")
    assert bb.add(_t(1000, 10)) == []
    bb.add(_t(2000, 12, 2, maker=False))
    bb.add(_t(3000, 9, 3, maker=True))
    closed = bb.add(_t(16_000, 11))  # skips bucket [5000, 10000) and [10000, 15000)
    assert [b.open_ms for b in closed] == [0, 5000, 10000]
    b0 = closed[0]
    assert (b0.open, b0.high, b0.low, b0.close) == (10, 12, 9, 9)
    assert b0.volume == 6 and b0.buy_volume == 3 and b0.delta == 0
    assert b0.vwap == pytest.approx((10 + 24 + 27) / 6)
    gap = closed[1]
    assert gap.volume == 0 and gap.open == gap.close == 9
    assert [b.open_ms for b in bb.flush(21_000)] == [15000]


def test_state_dedupes_and_detects_stale() -> None:
    st = MarketState()
    assert st.apply(_t(1000, 10, aid=5), 1000)
    assert not st.apply(_t(1001, 10, aid=5), 1001)  # overlap after reconnect
    book = parse_event(BOOK)
    assert book is not None
    st.apply(book, 10_000)
    assert st.stale_reasons("BTCUSDT", 12_000, 5, ("book",)) == []
    assert st.stale_reasons("BTCUSDT", 16_000, 5, ("book", "mark")) == ["book", "mark"]
    assert st.stale_reasons("NOPE", 0, 5, ("book",)) == ["book"]


def test_median_spread() -> None:
    st = MarketState()
    for i, spr in enumerate([1.0, 3.0, 2.0]):
        st.apply(BookTicker("X", i + 1, 100, 1, 100 + spr / 100, 1, 0), i * 1000)
    med = st.median_spread_bps("X", 0)
    assert med == pytest.approx(2.0, rel=1e-2)


def test_recorder_roundtrip(tmp_path: Path) -> None:
    rec = TickRecorder(tmp_path, flush_rows=10_000, flush_s=1e9)
    for i in range(50):
        rec.record(AggTrade("BTCUSDT", i, 100 + i, 1, 1791726018934 + i, False))
    d = parse_event(DEPTH)
    assert d is not None
    rec.record(d)
    rec.flush()
    t = load_ticks(tmp_path, "aggTrade", "BTCUSDT", "2026-10-11")
    assert t.num_rows == 50 and t.column("price")[49].as_py() == 149
    dt = load_ticks(tmp_path, "depth", "BTCUSDT", "2026-10-11")
    assert dt.column("b0_px")[0].as_py() == 100 and dt.column("a1_q")[0].as_py() == 0


def test_hub_dispatches_bars_and_events() -> None:
    hub = MarketDataHub("wss://x", ["5s"])
    hub.builders[("X", "5s")] = BarBuilder("X", "5s")
    bars: list[Bar] = []
    hub.bar_subscribers.append(bars.append)
    hub.handle(_t(1000, 10, aid=1), 1000)
    hub.handle(_t(6000, 11, aid=2), 6000)
    hub.handle(_t(6001, 11, aid=2), 6001)  # duplicate dropped
    assert len(bars) == 1 and hub.counts["AggTrade"] == 2
