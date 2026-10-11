from __future__ import annotations

from pathlib import Path

import numpy as np

from advantage_player.config.settings import GameCfg, load_config
from advantage_player.ledger.lifecycle import baseline_of
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.events import AggTrade, BookTicker, MarkPrice
from advantage_player.marketdata.recorder import TickRecorder
from advantage_player.sim.engine import GameEngine, SignalEvent
from advantage_player.sim.fills import Costs, Leg, TradeResult
from advantage_player.sim.replay import (
    evaluate,
    kline_to_bar,
    replay_bars,
    replay_ticks,
)
from advantage_player.strategies.base import Signal, StrategyContext, Target
from advantage_player.strategies.indicators import Regime, SeriesState, Snapshot

CFG = load_config("config.yaml")


class Every5:
    """Stub strategy: BUY every 5th bar it sees, stop 1 ATR, target 1R."""

    name = "S"

    def __init__(self) -> None:
        self.cfg = GameCfg(timeframes=["1m"], regimes=["TREND", "RANGE", "HIGH_VOL"], entry="taker")
        self.n = 0

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        self.n += 1
        if self.n % 5:
            return None
        e = ctx.bar.close
        return Signal("BUY", "market", e, e - 1.0, (Target(1.0, 1.0),), "S", time_stop_bars=3)


def _force_regime(monkeypatch: object, regime: Regime) -> None:
    def update(self: SeriesState, bar: Bar) -> Snapshot:
        self.bars.append(bar)
        return Snapshot(bar.close, bar.close, 0, 1.0, bar.close, 1.0, 0, 50, regime, 1)

    monkeypatch.setattr(SeriesState, "update", update)  # type: ignore[attr-defined]


def _engine(bar_mode: bool) -> GameEngine:
    eng = GameEngine(CFG, lambda s: Costs(), bar_mode=bar_mode, seed=1)
    eng.strategies = [("S", Every5())]
    return eng


def _bars(n: int, start: float = 100.0, step: float = 0.3) -> list[Bar]:
    out = []
    for i in range(n):
        o = start + i * step
        out.append(Bar("X", 60, i * 60_000, o, o + 0.5, o - 0.2, o + step, 10, 1000, 6, 3))
    return out


def test_engine_bar_mode_end_to_end(monkeypatch: object) -> None:
    _force_regime(monkeypatch, Regime.TREND)
    eng = _engine(bar_mode=True)
    results: list[TradeResult] = []
    sigs: list[SignalEvent] = []
    eng.result_hooks.append(results.append)
    eng.signal_hooks.append(sigs.append)
    replay_bars(eng, _bars(60))
    games = {r.game_id for r in results}
    assert "S|X|1m|TREND" in games
    assert "BASELINE_S|X|1m|TREND" in {s.game_id for s in sigs if s.baseline}
    first = next(r for r in results if r.game_id == "S|X|1m|TREND")
    sig_bar_close = first.signal_ms
    assert first.entry_ms > sig_bar_close  # entered on the NEXT bar, never the signal bar
    assert first.exit_reason in {"target", "stop", "time"}


def test_engine_skips_warmup_and_high_vol(monkeypatch: object) -> None:
    for regime in (Regime.WARMUP, Regime.HIGH_VOL, Regime.NONE):
        _force_regime(monkeypatch, regime)
        eng = _engine(bar_mode=True)
        sigs: list[SignalEvent] = []
        eng.signal_hooks.append(sigs.append)
        replay_bars(eng, _bars(30))
        assert sigs == []


def test_engine_one_position_per_game(monkeypatch: object) -> None:
    _force_regime(monkeypatch, Regime.TREND)
    eng = _engine(bar_mode=True)
    flat = [Bar("X", 60, i * 60_000, 100, 100.1, 99.9, 100, 1, 100, 0.5, 1) for i in range(12)]
    replay_bars(eng, flat)  # never hits stop/target; time stop = 3 bars
    assert sum(1 for g in eng.open if g.startswith("S|")) <= 1


def test_tick_replay_from_recorded_parquet(tmp_path: Path, monkeypatch: object) -> None:
    _force_regime(monkeypatch, Regime.TREND)
    rec = TickRecorder(tmp_path, flush_s=1e9)
    t0 = 1_791_676_800_000  # 2026-10-11 00:00 UTC
    px = 100.0
    for i in range(600):  # 10 minutes, one trade per second, slow uptrend
        px += 0.01
        ts = t0 + i * 1000
        rec.record(BookTicker("X", i + 1, px - 0.01, 1, px + 0.01, 1, ts))
        rec.record(AggTrade("X", i + 1, px, 1.0, ts, i % 3 == 0))
        if i % 60 == 0:
            rec.record(MarkPrice("X", ts, px, px, 0.0001, t0 + 8 * 3_600_000))
    rec.flush()
    eng = _engine(bar_mode=False)
    results: list[TradeResult] = []
    eng.result_hooks.append(results.append)
    n = replay_ticks(eng, tmp_path, "X", ["2026-10-11"])
    assert n == 600 * 2 + 10
    assert ("X", "1m") in eng.series and len(eng.series[("X", "1m")].bars) >= 8
    assert any(r.game_id.startswith("S|") for r in results)


def _tr(gid: str, r: float, ts: int) -> TradeResult:
    return TradeResult(
        gid,
        "X",
        "BUY",
        "t",
        ts,
        ts,
        ts + 1,
        100,
        100,
        99,
        [Leg(100 + r, 100 + r, 1, 0, "x")],
        r,
        r + 0.05,
        0.05,
        0,
        0,
        0,
        0,
        "target",
        False,
        None,
    )


def test_walk_forward_evaluate_and_bh() -> None:
    rng = np.random.default_rng(5)
    res = []
    for i in range(400):
        res.append(_tr("A|X|1m|TREND", float(rng.normal(0.4, 1)), i))
        res.append(_tr("BASELINE_A|X|1m|TREND", float(rng.normal(-0.05, 1)), i))
        res.append(_tr("C|X|1m|RANGE", float(rng.normal(0.0, 1)), i))
        res.append(_tr("BASELINE_C|X|1m|RANGE", float(rng.normal(0.0, 1)), i))
    ev = {e.game_id: e for e in evaluate(res, split_ms=200)}
    assert set(ev) == {"A|X|1m|TREND", "C|X|1m|RANGE"}  # baselines aren't games to promote
    assert ev["A|X|1m|TREND"].oos.n == 200 and ev["A|X|1m|TREND"].train.n == 200
    assert ev["A|X|1m|TREND"].passed
    assert not ev["C|X|1m|RANGE"].passed
    assert baseline_of("A|X|1m|TREND") == "BASELINE_A|X|1m|TREND"


def test_kline_to_bar_uses_taker_buy_volume() -> None:
    k = [0, "100", "101", "99", "100.5", "10", 59_999, "1005", 42, "7", "703.5", "0"]
    b = kline_to_bar("X", "1m", k)
    assert b.buy_volume == 7 and b.delta == 4 and b.n_trades == 42
