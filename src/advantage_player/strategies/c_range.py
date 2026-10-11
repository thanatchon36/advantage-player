"""C — Range fade (RANGE; 1m / 5m).

Price at VWAP ± kσ band + rejection candle (pin bar / engulfing) + fading volume into the band.
Target VWAP. Stop beyond the band-side extreme + buffer × ATR.
"""

from __future__ import annotations

import math

from advantage_player.config.settings import GameCfg
from advantage_player.strategies.base import Signal, StrategyContext, Target
from advantage_player.strategies.indicators import Regime
from advantage_player.strategies.patterns import engulfing, pin_bar


class RangeFade:
    name = "C"

    def __init__(self, cfg: GameCfg) -> None:
        self.cfg = cfg

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        s = ctx.snap
        if s.regime is not Regime.RANGE or math.isnan(s.atr) or s.atr <= 0 or s.vwap_sigma <= 0:
            return None
        if len(ctx.bars) < 4:
            return None
        bar, prev = ctx.bars[-1], ctx.bars[-2]
        k = self.cfg.param("band_sigma", 2.0)
        upper, lower = s.vwap + k * s.vwap_sigma, s.vwap - k * s.vwap_sigma
        if bar.high >= upper:
            d = -1  # fade the top
        elif bar.low <= lower:
            d = 1
        else:
            return None
        if not (pin_bar(bar, d) or engulfing(prev, bar, d)):
            return None
        prior = ctx.bars[-4:-1]
        if not bar.volume < sum(b.volume for b in prior) / len(prior):
            return None  # volume must fade into the band
        extreme = bar.low if d > 0 else bar.high
        stop = extreme - d * self.cfg.param("stop_atr_buffer", 0.2) * s.atr
        entry = bar.close
        risk = (entry - stop) * d
        reward = (s.vwap - entry) * d
        if risk <= 0 or reward <= 0:
            return None
        return Signal(
            side="BUY" if d > 0 else "SELL",
            entry_type="market" if self.cfg.entry == "taker" else "maker",
            entry_ref=entry,
            stop=stop,
            targets=(Target(reward / risk, 1.0),),
            tag="C:fade",
            atr=s.atr,
            time_stop_bars=int(self.cfg.param("time_stop_bars", 30)),
        )
