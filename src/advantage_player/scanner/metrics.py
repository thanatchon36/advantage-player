"""Pure table-quality math (no I/O) for the universe scanner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from advantage_player.config.settings import UniverseCfg


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14) -> float:
    """Wilder ATR over the series (returns NaN if too short)."""
    h, lo, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    if len(c) < n + 1:
        return float("nan")
    tr = np.maximum(h[1:] - lo[1:], np.maximum(abs(h[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
    a = float(tr[:n].mean())
    for x in tr[n:]:
        a = (a * (n - 1) + float(x)) / n
    return a


def depth_within_bps(
    bids: Sequence[tuple[float, float]], asks: Sequence[tuple[float, float]], bps: float
) -> tuple[float, float]:
    """USD notional resting within ±bps of mid on each side."""
    if not bids or not asks:
        return 0.0, 0.0
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * (1 - bps / 1e4), mid * (1 + bps / 1e4)
    b = sum(p * q for p, q in bids if p >= lo)
    a = sum(p * q for p, q in asks if p <= hi)
    return b, a


def wick_anomaly(
    high: Sequence[float], low: Sequence[float], recent: int = 60, k: float = 8.0
) -> bool:
    """True if any of the last `recent` bars has range > k × median bar range of the window."""
    r = np.asarray(high, float) - np.asarray(low, float)
    if len(r) == 0:
        return False
    med = float(np.median(r))
    return med > 0 and bool((r[-recent:] > k * med).any())


def correlation_clusters(returns: dict[str, np.ndarray], threshold: float) -> list[set[str]]:
    """Connected components of the graph where corr(a, b) > threshold (union-find)."""
    syms = sorted(returns)
    parent = {s: s for s in syms}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(syms):
        for b in syms[i + 1 :]:
            ra, rb = returns[a], returns[b]
            n = min(len(ra), len(rb))
            if n < 10:
                continue
            x, y = ra[-n:], rb[-n:]
            if x.std() == 0 or y.std() == 0:
                continue
            if float(np.corrcoef(x, y)[0, 1]) > threshold:
                parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for s in syms:
        groups.setdefault(find(s), set()).add(s)
    return sorted(groups.values(), key=lambda g: (-len(g), sorted(g)[0]))


@dataclass
class TableMetrics:
    symbol: str
    price: float
    quote_volume: float
    depth_bid_usd: float
    depth_ask_usd: float
    spread_bps: float
    tick_bps: float
    maker_fee_bps: float
    taker_fee_bps: float
    slippage_bps: float
    funding_rate: float
    atr_bps: dict[str, float] = field(default_factory=dict)  # by timeframe
    oi_change: float | None = None
    anomaly: bool = False

    def round_trip_cost_bps(self, entry: str) -> float:
        fee = self.maker_fee_bps if entry == "maker" else self.taker_fee_bps
        return 2 * fee + self.spread_bps + self.slippage_bps

    def move_to_cost(self, tf: str, entry: str) -> float:
        a = self.atr_bps.get(tf, float("nan"))
        c = self.round_trip_cost_bps(entry)
        return a / c if c > 0 and a == a else 0.0

    def score(self, game_tfs: Sequence[tuple[str, str]]) -> float:
        """Table quality = best move_to_cost over the (timeframe, entry) pairs games use."""
        return max((self.move_to_cost(tf, e) for tf, e in game_tfs), default=0.0)


def ineligibility(m: TableMetrics, cfg: UniverseCfg, score: float) -> list[str]:
    reasons = []
    if m.quote_volume < cfg.min_quote_volume_usd:
        reasons.append("quote_volume")
    if min(m.depth_bid_usd, m.depth_ask_usd) < cfg.min_depth_usd_10bps:
        reasons.append("depth")
    if m.tick_bps > cfg.max_tick_bps:
        reasons.append("tick_bps")
    if score < cfg.min_move_to_cost:
        reasons.append("move_to_cost")
    if abs(m.funding_rate) > cfg.max_abs_funding:
        reasons.append("funding")
    if m.anomaly:
        reasons.append("anomaly")
    return reasons
