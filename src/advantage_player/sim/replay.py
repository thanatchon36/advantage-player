"""RESEARCH replay with walk-forward evaluation (SPEC §5 lifecycle: RESEARCH → SHADOW).

Sources:
- klines (REST history, 1m/5m): bar mode; aggressive-buy volume from the kline's taker-buy
  base volume. Strategy B needs the book and is skipped in this mode.
- recorded ticks (Parquet from the recorder): tick mode through the same hub/engine path.

Parameters are never fitted here: the train window only reports in-sample stats for
comparison; promotion uses OOS stats only, with BH across all games evaluated together.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from advantage_player.exchange.rest import BinanceRest
from advantage_player.ledger.lifecycle import baseline_of
from advantage_player.ledger.stats import GameStats, benjamini_hochberg, compute_stats, passes
from advantage_player.marketdata.bars import Bar, BarBuilder, tf_seconds
from advantage_player.marketdata.events import AggTrade, BookTicker, Depth, MarketEvent, MarkPrice
from advantage_player.marketdata.hub import MarketDataHub
from advantage_player.sim.engine import GameEngine
from advantage_player.sim.fills import TradeResult


def kline_to_bar(symbol: str, tf: str, k: Sequence[Any]) -> Bar:
    """REST kline row → Bar. Index 9 = taker-buy base volume (aggressive buys)."""
    vol = float(k[5])
    return Bar(
        symbol=symbol,
        tf_s=tf_seconds(tf),
        open_ms=int(k[0]),
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=vol,
        quote_volume=float(k[7]),
        buy_volume=float(k[9]),
        n_trades=int(k[8]),
    )


async def fetch_klines(
    rest: BinanceRest, symbol: str, tf: str, start_ms: int, end_ms: int
) -> list[Bar]:
    bars: list[Bar] = []
    cur = start_ms
    while cur < end_ms:
        await rest.throttle()
        rows = await rest.public(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": tf, "startTime": cur, "endTime": end_ms, "limit": 1000},
        )
        if not rows:
            break
        bars += [kline_to_bar(symbol, tf, r) for r in rows]
        cur = int(rows[-1][0]) + tf_seconds(tf) * 1000
    # drop the still-forming last bar
    return [b for b in bars if b.close_ms <= end_ms]


def replay_bars(engine: GameEngine, bars: Iterable[Bar]) -> None:
    for b in sorted(bars, key=lambda b: (b.close_ms, b.tf_s)):
        engine.on_bar(b)


def _iter_kind(root: Path, kind: str, symbol: str, days: Sequence[str]) -> Iterator[MarketEvent]:
    for day in days:
        d = root / "ticks" / kind / day / symbol
        for f in sorted(d.glob("part-*.parquet")):
            for row in pq.read_table(f).to_pylist():
                yield _event(kind, row)


def _event(kind: str, r: dict[str, Any]) -> MarketEvent:
    if kind == "aggTrade":
        return AggTrade(
            r["symbol"], r["agg_id"], r["price"], r["qty"], r["trade_ms"], r["buyer_maker"]
        )
    if kind == "bookTicker":
        return BookTicker(
            r["symbol"],
            r["update_id"],
            r["bid"],
            r["bid_qty"],
            r["ask"],
            r["ask_qty"],
            r["event_ms"],
        )
    if kind == "markPrice":
        return MarkPrice(
            r["symbol"],
            r["event_ms"],
            r["mark"],
            r["index"],
            r["funding_rate"],
            r["next_funding_ms"],
        )
    lv = range(5)
    return Depth(
        r["symbol"],
        r["event_ms"],
        tuple((r[f"b{i}_px"], r[f"b{i}_q"]) for i in lv if not math.isnan(r[f"b{i}_px"])),
        tuple((r[f"a{i}_px"], r[f"a{i}_q"]) for i in lv if not math.isnan(r[f"a{i}_px"])),
    )


def _ts(ev: MarketEvent) -> int:
    return ev.trade_ms if isinstance(ev, AggTrade) else ev.event_ms


def replay_ticks(engine: GameEngine, root: str | Path, symbol: str, days: Sequence[str]) -> int:
    """Merge recorded streams by timestamp and run them through hub → engine (tick mode)."""
    root = Path(root)
    hub = MarketDataHub("replay://", engine.timeframes)
    hub.state = engine.state
    hub.event_subscribers.append(engine.on_event)
    hub.bar_subscribers.append(engine.on_bar)
    for tf in engine.timeframes:
        hub.builders[(symbol, tf)] = BarBuilder(symbol, tf)
    streams = [
        _iter_kind(root, k, symbol, days) for k in ("aggTrade", "bookTicker", "depth", "markPrice")
    ]
    n = 0
    last_flush = 0
    for ev in heapq.merge(*streams, key=_ts):
        ts = _ts(ev)
        hub.handle(ev, ts)
        if ts - last_flush >= 1000:
            hub.flush_bars(ts - 250)
            last_flush = ts
        n += 1
    return n


@dataclass(frozen=True)
class GameEvaluation:
    game_id: str
    train: GameStats
    oos: GameStats
    bh_significant: bool
    passed: bool


def evaluate(
    results: Iterable[TradeResult],
    split_ms: int,
    q: float = 0.10,
    conf: float = 0.95,
    n_boot: int = 2000,
) -> list[GameEvaluation]:
    """Walk-forward: stats on [.., split) are in-sample, [split, ..) out-of-sample.

    BH is applied across all non-baseline games evaluated in this window.
    """
    by_game: dict[str, list[TradeResult]] = defaultdict(list)
    for r in results:
        by_game[r.game_id].append(r)

    def st(rs: list[TradeResult], base: list[TradeResult] | None) -> GameStats:
        return compute_stats(
            [r.r_net for r in rs],
            [r.fee_r for r in rs],
            [r.r_gross for r in rs],
            [b.r_net for b in base] if base is not None else None,
            conf,
            n_boot,
        )

    games = sorted(g for g in by_game if not g.startswith("BASELINE_"))
    rows = []
    for g in games:
        tr = [r for r in by_game[g] if r.entry_ms < split_ms]
        oos = [r for r in by_game[g] if r.entry_ms >= split_ms]
        base_oos = [r for r in by_game.get(baseline_of(g), []) if r.entry_ms >= split_ms]
        base_tr = [r for r in by_game.get(baseline_of(g), []) if r.entry_ms < split_ms]
        rows.append((g, st(tr, base_tr), st(oos, base_oos)))
    sig = benjamini_hochberg([o.p_value for _, _, o in rows], q)
    return [
        GameEvaluation(g, t, o, s, passes(o, s)) for (g, t, o), s in zip(rows, sig, strict=True)
    ]
