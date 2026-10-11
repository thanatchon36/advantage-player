"""BASELINE — random-entry control group for one strategy (always SHADOW).

Same symbols, regimes, frequency, sizing and exit rules as the wrapped strategy; only the
side and timing are random. Frequency tracks the wrapped strategy's observed signal rate per
eligible bar (Beta(1, 49) prior ≈ 2%); stop distance reuses the wrapped strategy's recent
stop distances in ATR units, and the exits reuse its targets / BE / trail / time-stop rules.
"""

from __future__ import annotations

import math
import random
from collections import deque

from advantage_player.strategies.base import Signal, Strategy, StrategyContext


class RandomBaseline:
    def __init__(self, wrapped: Strategy, seed: int | None = None) -> None:
        self.wrapped = wrapped
        self.cfg = wrapped.cfg
        self.name = f"BASELINE_{wrapped.name}"
        self.rng = random.Random(seed)
        self.eligible_bars = 0
        self.wrapped_signals = 0
        self.templates: deque[tuple[float, Signal]] = deque(maxlen=50)  # (stop in ATR, signal)

    def observe(self, ctx: StrategyContext, sig: Signal | None) -> None:
        """Feed every bar the wrapped strategy saw in an allowed regime, and its output."""
        self.eligible_bars += 1
        if sig is not None:
            self.wrapped_signals += 1
            if sig.atr > 0:
                self.templates.append((sig.risk_per_unit / sig.atr, sig))

    @property
    def rate(self) -> float:
        return (self.wrapped_signals + 1) / (self.eligible_bars + 50)

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        s = ctx.snap
        if math.isnan(s.atr) or s.atr <= 0 or self.rng.random() >= self.rate:
            return None
        d = self.rng.choice((1, -1))
        if self.templates:
            stop_atr, tmpl = self.rng.choice(list(self.templates))
        else:
            stop_atr, tmpl = 1.0, None
        maker = bool(tmpl and tmpl.entry_type == "maker" and ctx.book)
        if maker and ctx.book is not None:
            entry = ctx.book.bid if d > 0 else ctx.book.ask
        else:
            entry = ctx.bar.close
        stop = entry - d * stop_atr * s.atr
        return Signal(
            side="BUY" if d > 0 else "SELL",
            entry_type="maker" if maker else "market",
            entry_ref=entry,
            stop=stop,
            targets=tmpl.targets if tmpl else (),
            tag=f"{self.name}:random",
            move_stop_to_be_after_first_target=bool(
                tmpl and tmpl.move_stop_to_be_after_first_target
            ),
            trail_atr=tmpl.trail_atr if tmpl else 0.0,
            atr=s.atr,
            time_stop_bars=tmpl.time_stop_bars if tmpl else 30,
        )
