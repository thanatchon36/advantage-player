"""B — Order-flow imbalance, maker-first (TREND / RANGE; 5s / 15s).

Top-5 book imbalance beyond threshold persisting ≥ persist_s, same-side aggressive delta in
the bar, and spread = 1 tick. Post-only entry at the touch, post-only TP, stop = k × ATR.
"""

from __future__ import annotations

import math

from advantage_player.config.settings import GameCfg
from advantage_player.strategies.base import Signal, StrategyContext, Target
from advantage_player.strategies.indicators import Regime


class OrderFlowImbalance:
    name = "B"

    def __init__(self, cfg: GameCfg) -> None:
        self.cfg = cfg

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        s = ctx.snap
        if s.regime not in (Regime.TREND, Regime.RANGE) or math.isnan(s.atr) or s.atr <= 0:
            return None
        if ctx.book is None or ctx.depth is None or ctx.tick_size <= 0:
            return None
        if ctx.book.ask - ctx.book.bid > ctx.tick_size * 1.5:  # spread must be exactly 1 tick
            return None
        thr = self.cfg.param("imbalance", 0.7)
        imb = ctx.depth.imbalance(5)
        d = 1 if imb > thr else -1 if imb < 1 - thr else 0
        if d == 0:
            return None
        since = ctx.imbalance_since_ms.get(d)
        if since is None or ctx.now_ms - since < self.cfg.param("persist_s", 3.0) * 1000:
            return None
        if ctx.bar.delta * d <= 0:
            return None
        entry = ctx.book.bid if d > 0 else ctx.book.ask
        stop = entry - d * self.cfg.param("stop_atr", 1.0) * s.atr
        return Signal(
            side="BUY" if d > 0 else "SELL",
            entry_type="maker" if self.cfg.entry == "maker" else "market",
            entry_ref=entry,
            stop=stop,
            targets=(Target(self.cfg.param("tp_r", 1.0), 1.0, maker=True),),
            tag=f"B:imb{imb:.2f}",
            atr=s.atr,
            time_stop_bars=int(self.cfg.param("time_stop_bars", 20)),
        )
