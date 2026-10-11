"""Entry decision: every gate in order, returning BET or NO_BET with a reason (SPEC §6–7)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from advantage_player.config.settings import AppConfig
from advantage_player.exchange.models import LeverageBrackets, SymbolInfo
from advantage_player.ledger.lifecycle import GameStatus
from advantage_player.risk.caps import OpenRisk, cap_room
from advantage_player.risk.discipline import Discipline, bet_mode
from advantage_player.risk.sizing import HALT, dd_multiplier, kelly_risk_fraction, size_order


@dataclass(frozen=True)
class Decision:
    bet: bool
    reason: str
    mode: str = ""
    f: float = 0.0
    qty: float = 0.0
    leverage: int = 0
    notional: float = 0.0
    margin: float = 0.0

    @staticmethod
    def no(reason: str, mode: str = "") -> Decision:
        return Decision(False, reason, mode)


@dataclass(frozen=True)
class EntryRequest:
    symbol: str
    direction: int
    entry: float
    stop: float
    status: GameStatus
    cluster: int
    mu_low: float  # conservative edge from real (PROBE+LIVE) trades
    e_r2: float


def decide(
    req: EntryRequest,
    cfg: AppConfig,
    discipline: Discipline,
    *,
    now_ms: int,
    bankroll: float,
    drawdown: float,
    daily_loss_frac: float,
    open_risks: Sequence[OpenRisk],
    blockers: Sequence[str],
    si: SymbolInfo | None,
    brackets: LeverageBrackets | None,
    risk_scale: float = 1.0,
) -> Decision:
    mode = bet_mode(req.status)
    if mode is None:
        return Decision.no(f"status_{req.status.value}")
    blk = discipline.entry_block(req.symbol, now_ms)
    if blk:
        return Decision.no(blk, mode)
    if dd_multiplier(drawdown, cfg.risk) == HALT:
        return Decision.no("drawdown_halt", mode)
    if blockers:
        return Decision.no(blockers[0], mode)
    if si is None or brackets is None or si.status != "TRADING":
        return Decision.no("symbol_unavailable", mode)
    room = cap_room(open_risks, req.symbol, req.direction, req.cluster, cfg.risk, daily_loss_frac)
    if room.allowed_f <= 0:
        return Decision.no(room.reason, mode)
    scale = min(max(risk_scale, 0.0), 1.0)
    if mode == "live":
        f = kelly_risk_fraction(req.mu_low, req.e_r2, drawdown, cfg.risk) * scale
        if f <= 0:
            return Decision.no("kelly_zero", mode)
    else:
        f = cfg.risk.probe_risk * scale
        if f <= 0:
            return Decision.no("risk_scale_zero", mode)
    f = min(f, room.allowed_f)
    sized = size_order(
        bankroll,
        f,
        req.entry,
        req.stop,
        si,
        brackets,
        cfg.target_leverage,
        cfg.risk,
        min_notional_ok=(mode == "probe"),
    )
    if sized is None:
        return Decision.no("below_min_notional_or_margin_cap", mode)
    if sized.risk_frac > room.allowed_f + 1e-12:
        return Decision.no(f"cap:{room.reason or 'risk'}", mode)
    return Decision(
        True,
        "ok",
        mode,
        sized.risk_frac,
        float(sized.qty),
        sized.leverage,
        sized.notional,
        sized.margin,
    )
