"""Bankroll & bet sizing (SPEC §6). Pure functions — no I/O.

Inputs are ONLY bankroll, ledger stats, drawdown and caps (discipline rule 1: no
martingale — nothing here can see a loss streak, so losses can only shrink size).

    f_kelly = μ_low / E[R²]
    f       = clamp(kelly_fraction × f_kelly × dd_multiplier, 0, max_risk_per_trade)
    qty     = bankroll × f / |entry − stop|          (floored to stepSize)
    L_safe  = 1 / (stop_dist / 0.6 + mmr + fee_buffer)
    L       = min(TARGET_LEVERAGE, bracket_max(notional), L_safe)
    margin  = notional / L ≤ 5 % of bankroll, else shrink
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from advantage_player.config.settings import RiskCfg
from advantage_player.exchange.models import LeverageBrackets, SymbolInfo

HALT = -1.0


def dd_multiplier(drawdown: float, cfg: RiskCfg) -> float:
    """1.0 below 5 % drawdown, 0.5 in [5 %, 10 %), HALT (−1) at ≥ 10 %."""
    if drawdown >= cfg.dd_halt_at:
        return HALT
    if drawdown >= cfg.dd_half_at:
        return 0.5
    return 1.0


def kelly_risk_fraction(mu_low: float, e_r2: float, drawdown: float, cfg: RiskCfg) -> float:
    """Fraction of bankroll to lose if the stop hits, for a LIVE game."""
    mult = dd_multiplier(drawdown, cfg)
    if mult == HALT or e_r2 <= 0 or not math.isfinite(mu_low) or mu_low <= 0:
        return 0.0
    f = cfg.kelly_fraction * (mu_low / e_r2) * mult
    return min(max(f, 0.0), cfg.max_risk_per_trade)


def derive_leverage(
    entry: float,
    stop: float,
    notional: float,
    brackets: LeverageBrackets,
    target_leverage: int,
    cfg: RiskCfg,
) -> int:
    """Leverage is derived, not chosen. Integer (exchange rule), ≥ 1, floored toward safety."""
    stop_dist = abs(entry - stop) / entry
    b = brackets.for_notional(notional)
    l_safe = 1.0 / (stop_dist / cfg.liq_stop_fraction + b.maint_margin_ratio + cfg.fee_buffer)
    lev = min(float(target_leverage), float(b.initial_leverage), l_safe)
    return max(1, math.floor(lev))


def liq_guard_ok(entry: float, stop: float, liq: float, cfg: RiskCfg) -> bool:
    """Stop must sit within `liq_stop_fraction` (60 %) of the distance to liquidation."""
    if liq <= 0:
        return True  # no liquidation price (e.g. fully collateralised)
    return abs(entry - stop) <= cfg.liq_stop_fraction * abs(entry - liq)


@dataclass(frozen=True)
class SizedOrder:
    qty: Decimal
    leverage: int
    notional: float
    margin: float
    risk_frac: float  # actual fraction of bankroll lost at the stop (after rounding/shrinking)


def size_order(
    bankroll: float,
    f: float,
    entry: float,
    stop: float,
    si: SymbolInfo,
    brackets: LeverageBrackets,
    target_leverage: int,
    cfg: RiskCfg,
    *,
    min_notional_ok: bool = False,
) -> SizedOrder | None:
    """Position size for risk fraction f. None ⇒ skip (too small / can't fit caps).

    `min_notional_ok`: PROBE sizing — bump up to the exchange minimum if f's size is below it,
    as long as the resulting risk stays ≤ max_risk_per_trade.
    """
    dist = abs(entry - stop)
    if bankroll <= 0 or f <= 0 or dist <= 0 or entry <= 0:
        return None
    qty = si.round_qty(bankroll * f / dist)
    if min_notional_ok and not si.meets_min_notional(qty, Decimal(str(entry))):
        need = max(si.min_qty, si.min_notional / Decimal(str(entry)))
        qty = (need / si.step_size).to_integral_value(rounding=ROUND_CEILING) * si.step_size
        if float(qty) * dist / bankroll > cfg.max_risk_per_trade:
            return None
    for _ in range(3):  # margin cap may shrink qty, which can change the bracket
        if qty <= 0 or not si.meets_min_notional(qty, Decimal(str(entry))):
            return None
        notional = float(qty) * entry
        try:
            lev = derive_leverage(entry, stop, notional, brackets, target_leverage, cfg)
        except ValueError:
            return None
        margin = notional / lev
        cap = cfg.max_margin_frac * bankroll
        if margin <= cap * (1 + 1e-9):
            return SizedOrder(qty, lev, notional, margin, float(qty) * dist / bankroll)
        qty = si.round_qty(cap * lev / entry)
    return None
