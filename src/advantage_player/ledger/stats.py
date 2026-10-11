"""Edge-ledger statistics (SPEC §5). Pure functions on arrays of net R-multiples.

- μ_low: one-sided lower confidence bound of mean R. Bootstrap (percentile of resampled
  means) when n ≥ BOOTSTRAP_MIN_N, else Student-t approximation.
- p-value for H0: μ ≤ 0 — one-sided one-sample t-test.
- Δ vs BASELINE: μ_game − μ_baseline with its own lower bound (independent bootstrap, or
  Welch-t fallback) and one-sided p-value.
- Benjamini-Hochberg across games evaluated in the same window.
- CUSUM edge-decay alarm on the R stream.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats as sps

BOOTSTRAP_MIN_N = 20


def _arr(r: Sequence[float] | np.ndarray) -> np.ndarray:
    return np.asarray(r, dtype=float)


def mean_lower_bound(
    r: Sequence[float] | np.ndarray, conf: float = 0.95, n_boot: int = 5000, seed: int = 0
) -> float:
    x = _arr(r)
    n = len(x)
    if n < 2:
        return -math.inf
    if n >= BOOTSTRAP_MIN_N:
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, n, size=(n_boot, n))
        means = x[idx].mean(axis=1)
        return float(np.quantile(means, 1 - conf))
    se = float(x.std(ddof=1)) / math.sqrt(n)
    return float(x.mean() - sps.t.ppf(conf, n - 1) * se)


def p_value_positive(r: Sequence[float] | np.ndarray) -> float:
    """One-sided p for H0: μ ≤ 0 (small p ⇒ evidence of positive edge)."""
    x = _arr(r)
    if len(x) < 2:
        return 1.0
    if float(x.std(ddof=1)) == 0:
        return 0.0 if x.mean() > 0 else 1.0
    return float(sps.ttest_1samp(x, 0.0, alternative="greater").pvalue)


def diff_lower_bound(
    a: Sequence[float] | np.ndarray,
    b: Sequence[float] | np.ndarray,
    conf: float = 0.95,
    n_boot: int = 5000,
    seed: int = 0,
) -> float:
    """Lower bound of mean(a) − mean(b)."""
    x, y = _arr(a), _arr(b)
    if len(x) < 2 or len(y) < 2:
        return -math.inf
    if min(len(x), len(y)) >= BOOTSTRAP_MIN_N:
        rng = np.random.default_rng(seed)
        mx = x[rng.integers(0, len(x), size=(n_boot, len(x)))].mean(axis=1)
        my = y[rng.integers(0, len(y), size=(n_boot, len(y)))].mean(axis=1)
        return float(np.quantile(mx - my, 1 - conf))
    vx, vy = x.var(ddof=1) / len(x), y.var(ddof=1) / len(y)
    se = math.sqrt(vx + vy)
    if se == 0:
        return float(x.mean() - y.mean())
    den = vx**2 / (len(x) - 1) + vy**2 / (len(y) - 1)
    dof = (vx + vy) ** 2 / den if den > 0 else 1e9
    return float(x.mean() - y.mean() - sps.t.ppf(conf, dof) * se)


def benjamini_hochberg(pvals: Sequence[float], q: float = 0.10) -> list[bool]:
    """Step-up BH: reject H_(i) for i ≤ k where k = max{i : p_(i) ≤ i·q/m}."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    k = 0
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= rank * q / m:
            k = rank
    rejected = set(order[:k])
    return [i in rejected for i in range(m)]


def max_consecutive_losses(r: Sequence[float] | np.ndarray) -> int:
    best = cur = 0
    for v in r:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def cusum_alarm(
    r: Sequence[float] | np.ndarray, mu0: float, sigma: float, k: float = 0.25, h: float = 5.0
) -> bool:
    """Lower one-sided CUSUM for a downward shift of the mean from mu0 (in σ units)."""
    if sigma <= 0:
        return False
    s = 0.0
    for v in r:
        s = max(0.0, s + (mu0 - v) / sigma - k)
        if s > h:
            return True
    return False


@dataclass(frozen=True)
class GameStats:
    n: int
    win_rate: float
    avg_win: float
    avg_loss: float
    mean: float
    std: float
    e_r2: float
    mu_low: float
    p_value: float
    profit_factor: float
    max_consec_losses: int
    fee_share: float  # total fees / total gross (1.0 when gross ≤ 0)
    delta_vs_baseline: float | None = None
    delta_low: float | None = None

    @property
    def kelly(self) -> float:
        """Kelly fraction for R-multiple outcomes using the conservative μ_low."""
        return self.mu_low / self.e_r2 if self.e_r2 > 0 and self.mu_low > 0 else 0.0


def compute_stats(
    r_net: Sequence[float] | np.ndarray,
    fee_r: Sequence[float] | np.ndarray | None = None,
    r_gross: Sequence[float] | np.ndarray | None = None,
    baseline: Sequence[float] | np.ndarray | None = None,
    conf: float = 0.95,
    n_boot: int = 5000,
) -> GameStats:
    x = _arr(r_net)
    n = len(x)
    if n == 0:
        return GameStats(0, 0, 0, 0, 0, 0, 0, -math.inf, 1.0, 0, 0, 1.0)
    wins, losses = x[x > 0], x[x <= 0]
    mu = float(x.mean())
    sd = float(x.std(ddof=1)) if n > 1 else 0.0
    gl = float(-losses.sum())
    pf = float(wins.sum()) / gl if gl > 0 else math.inf
    fs = 1.0
    if fee_r is not None and r_gross is not None:
        g = float(_arr(r_gross).sum())
        fs = float(_arr(fee_r).sum()) / g if g > 0 else 1.0
    d = dlow = None
    if baseline is not None and len(baseline) > 0:
        d = mu - float(_arr(baseline).mean())
        dlow = diff_lower_bound(x, baseline, conf, n_boot)
    return GameStats(
        n=n,
        win_rate=len(wins) / n,
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        mean=mu,
        std=sd,
        e_r2=sd**2 + mu**2,
        mu_low=mean_lower_bound(x, conf, n_boot),
        p_value=p_value_positive(x),
        profit_factor=pf,
        max_consec_losses=max_consecutive_losses(x),
        fee_share=fs,
        delta_vs_baseline=d,
        delta_low=dlow,
    )


def passes(s: GameStats, bh_significant: bool) -> bool:
    """SPEC §5: BH-significant AND μ_low > 0 AND lower bound of Δ-vs-baseline > 0."""
    return bool(bh_significant and s.mu_low > 0 and s.delta_low is not None and s.delta_low > 0)
