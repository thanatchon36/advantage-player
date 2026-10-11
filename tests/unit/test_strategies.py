from __future__ import annotations

import math

from advantage_player.config.settings import GameCfg, RegimeCfg, load_config
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.events import BookTicker, Depth
from advantage_player.strategies.a_trend import TrendPullback
from advantage_player.strategies.b_ofi import OrderFlowImbalance
from advantage_player.strategies.base import StrategyContext, Target, game_id
from advantage_player.strategies.baseline import RandomBaseline
from advantage_player.strategies.c_range import RangeFade
from advantage_player.strategies.indicators import Regime, SeriesState, Snapshot
from advantage_player.strategies.patterns import engulfing, inside_bar_breakout, pin_bar, trigger

CFG = load_config("config.yaml")


def B(o: float, h: float, lo: float, c: float, v: float = 10, buy: float = 6, t: int = 0) -> Bar:
    return Bar("X", 60, t, o, h, lo, c, v, v * c, buy, 5)


def snap(regime: Regime, d: int = 0, **kw: float) -> Snapshot:
    base = dict(
        ema_fast=100.0,
        ema_slow=99.0,
        ema_fast_slope_bps=5.0,
        atr=1.0,
        vwap=100.0,
        vwap_sigma=1.0,
        vol_z=2.0,
        rv_pctile=50.0,
    )
    base.update(kw)
    return Snapshot(regime=regime, trend_dir=d, **base)


def test_patterns() -> None:
    assert engulfing(B(101, 101.2, 99.8, 100), B(99.9, 101.5, 99.7, 101.3), 1)
    assert not engulfing(B(101, 101.2, 99.8, 100), B(99.9, 101.5, 99.7, 101.3), -1)
    assert engulfing(B(100, 101.2, 99.8, 101), B(101.1, 101.3, 99.5, 99.8), -1)
    assert pin_bar(B(100.8, 101, 98, 100.9), 1)  # hammer
    assert pin_bar(B(99.2, 102, 99, 99.1), -1)  # shooting star
    assert not pin_bar(B(99, 101, 99, 101), 1)
    assert inside_bar_breakout(
        B(100, 102, 98, 101), B(100.5, 101, 99.5, 100.8), B(101, 101.5, 100.8, 101.2), 1
    )
    t = trigger([B(100, 102, 98, 101), B(100.5, 101, 99.5, 100.8), B(101, 101.5, 100.8, 101.2)], 1)
    assert t == ("inside_breakout", 99.5)


def _ctx(bars: list[Bar], s: Snapshot, **kw: object) -> StrategyContext:
    return StrategyContext("X", "1m", bars, s, **kw)  # type: ignore[arg-type]


def test_A_trend_pullback_signal_and_filters() -> None:
    a = TrendPullback(CFG.games["A_trend_pullback"])
    bars = [
        B(100.6, 100.7, 100.1, 100.2),
        B(100.3, 100.4, 99.9, 100.0, buy=3),
        B(99.95, 100.9, 99.8, 100.8, v=30, buy=25),
    ]  # bullish engulfing at VWAP/EMA zone
    sig = a.on_bar(_ctx(bars, snap(Regime.TREND, 1)))
    assert sig is not None and sig.side == "BUY"
    assert sig.stop == min(b.low for b in bars[-2:]) - 0.2  # extreme − 0.2 ATR
    assert sig.targets == (Target(1.5, 0.5), Target(2.5, 0.5))
    assert sig.move_stop_to_be_after_first_target and sig.trail_atr == 1.0
    assert a.on_bar(_ctx(bars, snap(Regime.RANGE, 0))) is None  # regime gate
    assert a.on_bar(_ctx(bars, snap(Regime.TREND, 1, vol_z=1.0))) is None  # volume confirm
    sold = [*bars[:-1], B(99.95, 100.9, 99.8, 100.8, v=30, buy=5)]
    assert a.on_bar(_ctx(sold, snap(Regime.TREND, 1))) is None  # delta must agree
    far = snap(Regime.TREND, 1, vwap=110.0, ema_fast=110.0)
    assert a.on_bar(_ctx(bars, far)) is None  # no pullback into zone


def test_B_order_flow() -> None:
    b = OrderFlowImbalance(CFG.games["B_order_flow"])
    book = BookTicker("X", 1, 100.0, 5, 100.1, 1, 0)
    depth = Depth("X", 0, ((100.0, 9),), ((100.1, 1),))
    bars = [B(100, 100.1, 100, 100.1, buy=8)]
    ctx = _ctx(
        bars,
        snap(Regime.RANGE),
        book=book,
        depth=depth,
        tick_size=0.1,
        imbalance_since_ms={1: 0},
        now_ms=4000,
    )
    sig = b.on_bar(ctx)
    assert sig is not None and sig.side == "BUY" and sig.entry_type == "maker"
    assert sig.entry_ref == 100.0 and sig.stop == 99.0 and sig.targets[0].maker
    ctx.now_ms = 2000  # imbalance not persistent long enough (3 s)
    assert b.on_bar(ctx) is None
    ctx.now_ms = 4000
    ctx.book = BookTicker("X", 1, 100.0, 5, 100.3, 1, 0)  # spread 3 ticks
    assert b.on_bar(ctx) is None


def test_C_range_fade() -> None:
    c = RangeFade(CFG.games["C_range_fade"])
    prior = [B(101, 101.5, 100.5, 101, v=20) for _ in range(3)]
    star = B(101.95, 102.6, 101.9, 101.9, v=10)  # shooting star into +2σ band, fading volume
    sig = c.on_bar(_ctx([*prior, star], snap(Regime.RANGE)))
    assert sig is not None and sig.side == "SELL"
    assert sig.stop == 102.6 + 0.2
    assert sig.target_price(sig.targets[0]) == 100.0  # target VWAP
    loud = B(101.95, 102.6, 101.9, 101.9, v=50)
    assert c.on_bar(_ctx([*prior, loud], snap(Regime.RANGE))) is None
    assert c.on_bar(_ctx([*prior, star], snap(Regime.TREND, 1))) is None


def test_baseline_matches_frequency_and_exits() -> None:
    a = TrendPullback(CFG.games["A_trend_pullback"])
    base = RandomBaseline(a, seed=7)
    bars = [B(100, 100.5, 99.5, 100)] * 3
    ctx = _ctx(bars, snap(Regime.TREND, 1))
    from advantage_player.strategies.base import Signal

    tmpl = Signal(
        "BUY", "market", 100, 98.5, (Target(1.5, 0.5), Target(2.5, 0.5)), "A", True, 1.0, 1.0, 30
    )
    fired = sides = 0
    for i in range(20_000):
        base.observe(ctx, tmpl if i % 20 == 0 else None)  # wrapped fires 5% of bars
        s = base.on_bar(ctx)
        if s:
            fired += 1
            sides += s.direction
            assert s.targets == tmpl.targets and s.move_stop_to_be_after_first_target
            assert math.isclose(s.risk_per_unit, 1.5)
    assert 0.04 < fired / 20_000 < 0.06
    assert abs(sides) < 0.1 * fired  # random side


def test_regime_classifier_trend_and_warmup() -> None:
    cfg = RegimeCfg(trend_slope_bps=0.5)
    s = SeriesState("X", 60, cfg, rv_window=5)
    first = None
    for i in range(400):
        px = 100 * (1.0005**i)
        noise = 0.05 if i % 2 else -0.05
        snap_ = s.update(
            Bar("X", 60, i * 60_000, px, px + 0.1, px - 0.1, px + noise, 10, 1000, 5, 3)
        )
        if first is None and snap_.regime is not Regime.WARMUP:
            first = i
    assert first is not None and first >= 50
    assert s.snapshot is not None and s.snapshot.regime is Regime.TREND
    assert s.snapshot.trend_dir == 1 and s.snapshot.atr > 0


def test_game_id_format() -> None:
    assert game_id("A", "BTCUSDT", "1m", Regime.TREND) == "A|BTCUSDT|1m|TREND"
    assert GameCfg(timeframes=["1m"], regimes=["TREND"], zone_atr=0.4).param("zone_atr", 0) == 0.4
