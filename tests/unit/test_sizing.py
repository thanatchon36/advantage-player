from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from advantage_player.config.settings import RiskCfg
from advantage_player.exchange.models import LeverageBrackets, SymbolInfo
from advantage_player.risk.caps import OpenRisk, cap_room
from advantage_player.risk.sizing import (
    HALT,
    dd_multiplier,
    derive_leverage,
    kelly_risk_fraction,
    liq_guard_ok,
    size_order,
)

R = RiskCfg()
SI = SymbolInfo(
    "BTCUSDT",
    "TRADING",
    "PERPETUAL",
    "USDT",
    0,
    4133404802000,
    Decimal("0.1"),
    Decimal("0.001"),
    Decimal("0.001"),
    Decimal("1000"),
    Decimal("0.001"),
    Decimal("120"),
    Decimal("50"),
)
BR = LeverageBrackets.from_api(
    {
        "symbol": "BTCUSDT",
        "brackets": [
            {
                "bracket": 1,
                "initialLeverage": 125,
                "notionalCap": 50_000,
                "notionalFloor": 0,
                "maintMarginRatio": 0.004,
                "cum": 0,
            },
            {
                "bracket": 2,
                "initialLeverage": 100,
                "notionalCap": 600_000,
                "notionalFloor": 50_000,
                "maintMarginRatio": 0.005,
                "cum": 50,
            },
            {
                "bracket": 3,
                "initialLeverage": 20,
                "notionalCap": 10_000_000,
                "notionalFloor": 600_000,
                "maintMarginRatio": 0.025,
                "cum": 12_050,
            },
        ],
    }
)


def test_dd_multiplier_steps() -> None:
    assert dd_multiplier(0.0, R) == 1.0
    assert dd_multiplier(0.0499, R) == 1.0
    assert dd_multiplier(0.05, R) == 0.5
    assert dd_multiplier(0.0999, R) == 0.5
    assert dd_multiplier(0.10, R) == HALT


def test_kelly_formula_and_clamp() -> None:
    # μ_low 0.2, E[R²] 2.0 → f_kelly 0.1 → ×0.25 = 0.025 → clamp to 1 %
    assert kelly_risk_fraction(0.2, 2.0, 0.0, R) == pytest.approx(0.01)
    # μ_low 0.02, E[R²] 2 → 0.01 × 0.25 = 0.0025
    assert kelly_risk_fraction(0.02, 2.0, 0.0, R) == pytest.approx(0.0025)
    assert kelly_risk_fraction(0.02, 2.0, 0.06, R) == pytest.approx(0.00125)  # dd ×0.5
    assert kelly_risk_fraction(0.02, 2.0, 0.10, R) == 0.0  # halt
    assert kelly_risk_fraction(-0.1, 2.0, 0.0, R) == 0.0  # no edge → no bet
    assert kelly_risk_fraction(float("-inf"), 2.0, 0.0, R) == 0.0


def test_derived_leverage() -> None:
    # 1 % stop: L_safe = 1/(0.01/0.6 + 0.004 + 0.002) = 44.1 → 44 (< target 50 < bracket 125)
    assert derive_leverage(100, 99, 10_000, BR, 50, R) == 44
    # 0.1 % stop: L_safe ≈ 132 → capped by TARGET_LEVERAGE 50
    assert derive_leverage(100, 99.9, 10_000, BR, 50, R) == 50
    # big notional → bracket 3 max 20x
    assert derive_leverage(100, 99.9, 700_000, BR, 50, R) == 20
    # wide 10 % stop → L_safe ≈ 5.8 → 5
    assert derive_leverage(100, 90, 10_000, BR, 50, R) == 5


def test_size_order_basic_and_rounding() -> None:
    s = size_order(10_000, 0.01, 60_000, 59_400, SI, BR, 50, R)  # risk $100, dist $600
    assert s is not None
    assert s.qty == Decimal("0.166")  # 0.1666 floored to step
    assert s.risk_frac <= 0.01
    assert s.margin <= 0.05 * 10_000
    assert s.leverage <= 50


def test_margin_cap_shrinks_size() -> None:
    # tiny stop distance → huge qty; margin cap 5 % must shrink it
    s = size_order(10_000, 0.01, 60_000, 59_990, SI, BR, 50, R)
    assert s is not None
    assert s.margin <= 500 + 1e-6
    assert s.risk_frac < 0.01


def test_below_min_notional_skips_but_probe_bumps() -> None:
    assert size_order(1_000, 0.0001, 60_000, 59_000, SI, BR, 50, R) is None
    p = size_order(1_000, 0.001, 60_000, 59_400, SI, BR, 50, R, min_notional_ok=True)
    assert p is not None and p.qty * Decimal(60_000) >= 50
    # probe bump refused when min size would exceed max_risk_per_trade
    assert size_order(100, 0.001, 60_000, 50_000, SI, BR, 50, R, min_notional_ok=True) is None


def test_liq_guard() -> None:
    assert liq_guard_ok(100, 98, 95, R)  # 2 ≤ 0.6 × 5
    assert not liq_guard_ok(100, 96, 95, R)  # 4 > 3


@settings(max_examples=400, deadline=None)
@given(
    bankroll=st.floats(10, 1e7),
    mu_low=st.floats(-1, 3),
    e_r2=st.floats(0.01, 20),
    dd=st.floats(0, 0.2),
    entry=st.floats(100, 200_000),
    stop_pct=st.floats(0.0005, 0.2),
    side=st.sampled_from([1, -1]),
    target=st.integers(1, 125),
)
def test_property_sizing_never_exceeds_any_cap(
    bankroll: float,
    mu_low: float,
    e_r2: float,
    dd: float,
    entry: float,
    stop_pct: float,
    side: int,
    target: int,
) -> None:
    stop = entry * (1 - side * stop_pct)
    f = kelly_risk_fraction(mu_low, e_r2, dd, R)
    assert 0 <= f <= R.max_risk_per_trade
    if dd >= R.dd_halt_at:
        assert f == 0
    s = size_order(bankroll, f, entry, stop, SI, BR, target, R)
    if s is None:
        return
    assert s.risk_frac <= f + 1e-12 <= R.max_risk_per_trade + 1e-12
    assert s.margin <= R.max_margin_frac * bankroll * (1 + 1e-9)
    assert 1 <= s.leverage <= min(target, BR.max_leverage(s.notional))
    assert (s.qty / SI.step_size) == (s.qty / SI.step_size).to_integral_value()
    assert SI.meets_min_notional(s.qty, Decimal(str(entry)))


@settings(max_examples=300, deadline=None)
@given(
    fs=st.lists(
        st.tuples(st.floats(0.0001, 0.01), st.sampled_from([1, -1]), st.integers(-1, 3)), max_size=6
    ),
    direction=st.sampled_from([1, -1]),
    cluster=st.integers(-1, 3),
)
def test_property_caps_never_exceeded(
    fs: list[tuple[float, int, int]], direction: int, cluster: int
) -> None:
    pos = [OpenRisk(f"S{i}", d, f, c) for i, (f, d, c) in enumerate(fs)]
    assume(sum(p.f for p in pos) <= R.max_total_open_risk)
    room = cap_room(pos, "NEW", direction, cluster, R).allowed_f
    if room <= 0:
        return
    new = [*pos, OpenRisk("NEW", direction, room, cluster)]
    assert len(new) <= R.max_positions
    assert sum(p.f for p in new) <= R.max_total_open_risk + 1e-12
    if cluster >= 0:
        assert sum(p.f for p in new if p.cluster == cluster) <= R.max_cluster_risk + 1e-12
    net_before = sum(p.f * p.direction for p in pos)
    net_after = net_before + direction * room
    assert direction * net_after <= R.max_net_direction_risk + 1e-12
    assert room <= R.max_risk_per_trade


def test_caps_each_rule() -> None:
    assert cap_room([], "X", 1, 0, R, daily_loss_frac=0.03).reason == "daily_loss_limit"
    assert cap_room([OpenRisk("X", 1, 0.001, 0)], "X", 1, 0, R).reason == "position_exists"
    four = [OpenRisk(f"S{i}", 1, 0.001, i) for i in range(4)]
    assert cap_room(four, "X", 1, 9, R).reason == "max_positions"
    c = cap_room([OpenRisk("A", 1, 0.01, 0), OpenRisk("B", -1, 0.01, 1)], "X", 1, 2, R)
    assert c.allowed_f == pytest.approx(0.01) and c.reason == ""  # total room 1 %
    c = cap_room([OpenRisk("A", 1, 0.01, 0)], "X", 1, 0, R)
    assert c.allowed_f == pytest.approx(0.005) and c.reason == "cluster_risk"
    c = cap_room([OpenRisk("A", 1, 0.01, 0), OpenRisk("B", 1, 0.008, 1)], "X", 1, 2, R)
    assert c.allowed_f == pytest.approx(0.002) and c.reason == "net_direction"
    c = cap_room([OpenRisk("A", 1, 0.01, 0), OpenRisk("B", 1, 0.008, 1)], "X", -1, 2, R)
    assert c.allowed_f == pytest.approx(0.01)  # opposite side reduces net exposure
