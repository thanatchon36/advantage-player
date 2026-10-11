"""Incremental per-(symbol, timeframe) indicators and regime classification (SPEC §4).

- EMA20/EMA50, Wilder ATR14, volume z-score (50 bars)
- VWAP anchored at 00:00 UTC with volume-weighted σ of typical price (bands)
- realized vol over `rv_window` bars, sampled every `rv_window` bars into a 30-day history;
  HIGH_VOL when the current value is above the configured percentile of that history.
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from advantage_player.config.settings import RegimeCfg
from advantage_player.marketdata.bars import Bar

DAY_MS = 86_400_000


class Regime(StrEnum):
    WARMUP = "WARMUP"
    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    NONE = "NONE"


@dataclass(frozen=True)
class Snapshot:
    """Indicator values after the latest closed bar."""

    ema_fast: float
    ema_slow: float
    ema_fast_slope_bps: float
    atr: float
    vwap: float
    vwap_sigma: float
    vol_z: float
    rv_pctile: float | None
    regime: Regime
    trend_dir: int  # +1 up, -1 down, 0 none


def _ema_alpha(n: int) -> float:
    return 2 / (n + 1)


class SeriesState:
    MIN_RV_HISTORY = 50

    def __init__(self, symbol: str, tf_s: int, cfg: RegimeCfg, rv_window: int = 20) -> None:
        self.symbol = symbol
        self.tf_s = tf_s
        self.cfg = cfg
        self.bars: deque[Bar] = deque(maxlen=500)
        self.n = 0
        self.ema_fast = math.nan
        self.ema_slow = math.nan
        self.prev_ema_fast = math.nan
        self.atr = math.nan
        self._tr_seed: list[float] = []
        self._vols: deque[float] = deque(maxlen=50)
        self._vwap_day = -1
        self._pv = self._v = self._p2v = 0.0
        self.rv_window = rv_window
        self._rets: deque[float] = deque(maxlen=rv_window)
        hist_len = max(1, (30 * DAY_MS) // (tf_s * 1000 * rv_window))
        self.rv_history: deque[float] = deque(maxlen=int(hist_len))
        self.snapshot: Snapshot | None = None

    def update(self, bar: Bar) -> Snapshot:
        prev = self.bars[-1] if self.bars else None
        self.bars.append(bar)
        self.n += 1
        c = bar.close
        # EMAs
        self.prev_ema_fast = self.ema_fast
        if math.isnan(self.ema_fast):
            self.ema_fast = self.ema_slow = c
        else:
            self.ema_fast += _ema_alpha(self.cfg.ema_fast) * (c - self.ema_fast)
            self.ema_slow += _ema_alpha(self.cfg.ema_slow) * (c - self.ema_slow)
        # ATR (Wilder, 14)
        if prev is not None:
            tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
            if math.isnan(self.atr):
                self._tr_seed.append(tr)
                if len(self._tr_seed) == 14:
                    self.atr = sum(self._tr_seed) / 14
            else:
                self.atr = (self.atr * 13 + tr) / 14
            if prev.close > 0 and c > 0:
                self._rets.append(math.log(c / prev.close))
        # volume z
        vols = list(self._vols)
        vol_z = 0.0
        if len(vols) >= 20:
            sd = float(np.std(vols))
            vol_z = (bar.volume - float(np.mean(vols))) / sd if sd > 0 else 0.0
        self._vols.append(bar.volume)
        # daily-anchored VWAP + σ
        day = bar.open_ms // DAY_MS
        if day != self._vwap_day:
            self._vwap_day, self._pv, self._v, self._p2v = day, 0.0, 0.0, 0.0
        tp = (bar.high + bar.low + bar.close) / 3
        w = bar.volume
        self._pv += tp * w
        self._v += w
        self._p2v += tp * tp * w
        vwap = self._pv / self._v if self._v > 0 else c
        var = self._p2v / self._v - vwap * vwap if self._v > 0 else 0.0
        sigma = math.sqrt(max(var, 0.0))
        # realized vol + percentile
        rv_pct: float | None = None
        if len(self._rets) == self.rv_window:
            rv = float(np.std(self._rets))
            hist = self.rv_history
            if len(hist) >= self.MIN_RV_HISTORY:
                rv_pct = 100.0 * sum(1 for h in hist if h < rv) / len(hist)
            if self.n % self.rv_window == 0:
                hist.append(rv)
        slope_bps = (
            (self.ema_fast - self.prev_ema_fast) / c * 1e4
            if c > 0 and not math.isnan(self.prev_ema_fast)
            else 0.0
        )
        regime, tdir = self._classify(slope_bps, vwap, rv_pct)
        self.snapshot = Snapshot(
            self.ema_fast,
            self.ema_slow,
            slope_bps,
            self.atr,
            vwap,
            sigma,
            vol_z,
            rv_pct,
            regime,
            tdir,
        )
        return self.snapshot

    def _classify(self, slope_bps: float, vwap: float, rv_pct: float | None) -> tuple[Regime, int]:
        if self.n < self.cfg.ema_slow or math.isnan(self.atr) or rv_pct is None:
            return Regime.WARMUP, 0
        if rv_pct > self.cfg.high_vol_pctile:
            return Regime.HIGH_VOL, 0
        thr = self.cfg.trend_slope_bps
        if self.ema_fast > self.ema_slow and slope_bps > thr:
            return Regime.TREND, 1
        if self.ema_fast < self.ema_slow and slope_bps < -thr:
            return Regime.TREND, -1
        if abs(slope_bps) <= thr and self._vwap_crossings(vwap) >= 2:
            return Regime.RANGE, 0
        return Regime.NONE, 0

    def _vwap_crossings(self, vwap: float, lookback: int = 20) -> int:
        side = [1 if b.close > vwap else -1 for b in list(self.bars)[-lookback:]]
        return sum(1 for a, b in itertools.pairwise(side) if a != b)
