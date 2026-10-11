from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from advantage_player.ledger.stats import (
    benjamini_hochberg,
    compute_stats,
    cusum_alarm,
    diff_lower_bound,
    max_consecutive_losses,
    mean_lower_bound,
    p_value_positive,
    passes,
)


def test_lower_bound_below_mean_and_t_fallback() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.3, 1.0, 500)
    lb = mean_lower_bound(x)
    assert lb < x.mean()
    se = x.std(ddof=1) / math.sqrt(len(x))
    assert lb == pytest.approx(x.mean() - 1.645 * se, abs=0.02)  # bootstrap ≈ normal theory
    small = [1.0, -1.0, 2.0, 0.5]
    from scipy import stats as sps

    expect = np.mean(small) - sps.t.ppf(0.95, 3) * np.std(small, ddof=1) / 2
    assert mean_lower_bound(small) == pytest.approx(expect)
    assert mean_lower_bound([1.0]) == -math.inf


def test_p_value_direction() -> None:
    rng = np.random.default_rng(2)
    assert p_value_positive(rng.normal(0.5, 1, 200)) < 0.001
    assert p_value_positive(rng.normal(-0.5, 1, 200)) > 0.99


def test_bh_known_example() -> None:
    # m=5, q=0.1 → thresholds 0.02, 0.04, 0.06, 0.08, 0.10
    p = [0.01, 0.045, 0.03, 0.5, 0.07]
    # sorted: .01(≤.02) .03(≤.04) .045(≤.06) .07(≤.08) .5(>.1) → k=4
    assert benjamini_hochberg(p, 0.1) == [True, True, True, False, True]
    assert benjamini_hochberg([0.2, 0.3], 0.1) == [False, False]
    assert benjamini_hochberg([], 0.1) == []


@given(st.lists(st.floats(0, 1), min_size=1, max_size=40))
def test_bh_controls_more_conservatively_than_raw(p: list[float]) -> None:
    rej = benjamini_hochberg(p, 0.1)
    assert all(pv <= 0.1 for pv, r in zip(p, rej, strict=True) if r)


def test_diff_lower_bound() -> None:
    rng = np.random.default_rng(3)
    a, b = rng.normal(0.4, 1, 400), rng.normal(0.0, 1, 400)
    assert 0 < diff_lower_bound(a, b) < 0.4
    assert diff_lower_bound([1, 2, 3], [0, 1]) < 2  # Welch path


def test_consecutive_losses_and_cusum() -> None:
    assert max_consecutive_losses([1, -1, -1, 2, -1, -1, -1, 0.5]) == 3
    rng = np.random.default_rng(4)
    good = rng.normal(0.3, 1, 300)
    assert not cusum_alarm(good, mu0=0.3, sigma=1.0)
    decayed = np.concatenate([good, rng.normal(-0.7, 1, 100)])
    assert cusum_alarm(decayed, mu0=0.3, sigma=1.0)


def test_compute_stats_and_kelly_and_pass() -> None:
    r = [2.0, -1.0, 2.0, -1.0, 2.0] * 40
    s = compute_stats(r, fee_r=[0.1] * 200, r_gross=[x + 0.1 for x in r], baseline=[-0.1, 0.1] * 50)
    assert s.n == 200 and s.win_rate == pytest.approx(0.6)
    assert s.mean == pytest.approx(0.8)
    assert s.e_r2 == pytest.approx(s.std**2 + s.mean**2)
    assert s.profit_factor == pytest.approx(6 / 2)  # per 5 trades: wins 3×2R, losses 2×1R
    assert s.kelly == pytest.approx(s.mu_low / s.e_r2)
    assert 0 < s.kelly < s.mean / s.e_r2  # conservative μ_low < μ
    assert s.fee_share == pytest.approx(20 / (160 + 20))
    assert passes(s, True)
    assert not passes(s, False)
    neg = compute_stats([-0.1, 0.05] * 50)
    assert neg.kelly == 0.0 and not passes(neg, True)
    assert compute_stats([]).n == 0
