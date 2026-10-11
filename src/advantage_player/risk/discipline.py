"""Discipline rules (SPEC §7). Hard-coded; neither the bot nor the human can break them.

State is persisted by the caller (kv) so a restart can't be used to escape a lock or halt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from advantage_player.config.settings import DisciplineCfg
from advantage_player.ledger.lifecycle import GameStatus

MIN = 60_000
HOUR = 3_600_000
STOP_EXITS = {"stop", "be_stop", "trail_stop"}


class DisciplineViolationError(RuntimeError):
    pass


def bet_mode(status: GameStatus) -> str | None:
    """Rule 6: LIVE → normal size, PROBE → probe size, anything else → no trade."""
    if status is GameStatus.LIVE:
        return "live"
    if status is GameStatus.PROBE:
        return "probe"
    return None


def validate_sl_move(direction: int, old_stop: float, new_stop: float) -> None:
    """Rule 4: an SL may only move toward profit (never widened)."""
    if (new_stop - old_stop) * direction < 0:
        raise DisciplineViolationError(
            f"SL widening refused: {old_stop} → {new_stop} for direction {direction}"
        )


@dataclass
class DisciplineState:
    symbol_locks: dict[str, int] = field(default_factory=dict)  # symbol → locked until (ms)
    loss_streak: int = 0
    streak_games: list[str] = field(default_factory=list)
    paused_until_ms: int = 0  # automatic pause (loss streak)
    manual_pause: bool = False  # /pause
    killed_at_ms: int | None = None  # rule 7 hard kill
    halted: bool = False

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> DisciplineState:
        return cls(**d) if d else cls()


class Discipline:
    def __init__(self, cfg: DisciplineCfg, state: DisciplineState | None = None) -> None:
        self.cfg = cfg
        self.state = state or DisciplineState()

    # ---- rule 5: no revenge -----------------------------------------------------------------
    def on_trade_closed(
        self, symbol: str, game_id: str, r_net: float, exit_reason: str, now_ms: int
    ) -> list[str]:
        """Returns game ids needing an edge-decay check when a loss-streak pause triggers."""
        s = self.state
        if exit_reason in STOP_EXITS:
            s.symbol_locks[symbol] = now_ms + self.cfg.symbol_lock_after_stop_min * MIN
        if r_net < 0:
            s.loss_streak += 1
            s.streak_games.append(game_id)
        else:
            s.loss_streak = 0
            s.streak_games = []
        if s.loss_streak >= self.cfg.loss_streak_pause_n:
            s.paused_until_ms = now_ms + self.cfg.loss_streak_pause_min * MIN
            games = sorted(set(s.streak_games))
            s.loss_streak, s.streak_games = 0, []
            return games
        return []

    # ---- rule 7: hard kill ------------------------------------------------------------------
    def kill(self, now_ms: int) -> None:
        self.state.killed_at_ms = now_ms
        self.state.halted = True

    def resume(self, now_ms: int, drawdown: float, dd_halt_at: float) -> str:
        """Manual /resume. Kill can't be lifted before 12 h, nor while drawdown ≥ halt level."""
        s = self.state
        if s.halted and s.killed_at_ms is not None:
            wait = s.killed_at_ms + self.cfg.kill_resume_min_hours * HOUR - now_ms
            if wait > 0:
                return f"refused: kill resume allowed in {wait / HOUR:.1f} h"
            if drawdown >= dd_halt_at:
                return f"refused: drawdown {drawdown:.1%} still ≥ {dd_halt_at:.0%}"
            s.halted, s.killed_at_ms = False, None
        s.manual_pause = False
        return "resumed"

    def pause(self) -> None:
        self.state.manual_pause = True

    # ---- gate -------------------------------------------------------------------------------
    def entry_block(self, symbol: str, now_ms: int) -> str | None:
        s = self.state
        if s.halted:
            return "halted_kill"
        if s.manual_pause:
            return "paused_manual"
        if now_ms < s.paused_until_ms:
            return "paused_loss_streak"
        if now_ms < s.symbol_locks.get(symbol, 0):
            return "symbol_locked_after_stop"
        return None


def entry_blockers(
    cfg: DisciplineCfg,
    *,
    now_ms: int,
    stale: list[str],
    ws_healthy: bool,
    spread_bps: float | None,
    regime: str,
    next_funding_ms: int | None,
    last_funding_ms: int | None,
    clock_drift_ms: float,
    rate_headroom: float,
    allow_high_vol: bool = False,
) -> list[str]:
    """Rule 10: every condition that forbids a new entry right now."""
    out = []
    if stale:
        out.append("stale_data:" + ",".join(stale))
    if not ws_healthy:
        out.append("ws_down")
    if spread_bps is None or spread_bps > cfg.max_spread_bps:
        out.append("spread")
    if regime == "HIGH_VOL" and not allow_high_vol:
        out.append("high_vol")
    m = cfg.funding_blackout_min * MIN
    near_next = next_funding_ms is not None and 0 <= next_funding_ms - now_ms <= m
    near_last = last_funding_ms is not None and 0 <= now_ms - last_funding_ms <= m
    if near_next or near_last:
        out.append("funding_window")
    if clock_drift_ms > cfg.max_clock_drift_ms:
        out.append("clock_drift")
    if rate_headroom < cfg.min_rate_headroom:
        out.append("rate_limit_headroom")
    return out
