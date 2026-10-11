"""A — Trend pullback + candlestick trigger (TREND; 15s / 1m / 5m)."""

from __future__ import annotations

import math

from advantage_player.config.settings import GameCfg
from advantage_player.strategies.base import Signal, StrategyContext, Target
from advantage_player.strategies.indicators import Regime
from advantage_player.strategies.patterns import trigger


class TrendPullback:
    name = "A"

    def __init__(self, cfg: GameCfg) -> None:
        self.cfg = cfg

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        s = ctx.snap
        d = s.trend_dir
        if s.regime is not Regime.TREND or d == 0 or math.isnan(s.atr) or s.atr <= 0:
            return None
        bar = ctx.bar
        zone = self.cfg.param("zone_atr", 0.3) * s.atr
        # Pullback: this bar or the previous one traded into the VWAP / EMA20 zone.
        recent = ctx.bars[-2:]
        touched = any(
            (b.low <= lvl + zone and b.high >= lvl - zone)
            for b in recent
            for lvl in (s.vwap, s.ema_fast)
        )
        if not touched:
            return None
        trig = trigger(ctx.bars, d)
        if trig is None:
            return None
        if s.vol_z <= self.cfg.param("vol_z", 1.5) or bar.delta * d <= 0:
            return None
        pattern, extreme = trig
        stop = extreme - d * self.cfg.param("stop_atr_buffer", 0.2) * s.atr
        entry = bar.close
        if (entry - stop) * d <= 0:
            return None
        tp1_frac = self.cfg.param("tp1_fraction", 0.5)
        return Signal(
            side="BUY" if d > 0 else "SELL",
            entry_type="market" if self.cfg.entry == "taker" else "maker",
            entry_ref=entry,
            stop=stop,
            targets=(
                Target(self.cfg.param("tp1_r", 1.5), tp1_frac),
                Target(self.cfg.param("tp2_r", 2.5), 1 - tp1_frac),
            ),
            tag=f"A:{pattern}",
            move_stop_to_be_after_first_target=True,
            trail_atr=self.cfg.param("trail_atr", 1.0),
            atr=s.atr,
            time_stop_bars=int(self.cfg.param("time_stop_bars", 30)),
        )
