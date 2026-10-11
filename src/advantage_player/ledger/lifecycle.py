"""Game lifecycle (SPEC §5): RESEARCH → SHADOW → PROBE → LIVE → SUSPENDED → SHADOW.

Persisted per environment; DEMO and LIVE never share state. Every transition is stored and
pushed to `transition_hooks` (Telegram in Phase 6).

Promotion windows use only trades taken in the current stage since entering it:
- SHADOW → PROBE: n ≥ shadow_min_n and PASS (BH across all SHADOW games evaluated now).
- PROBE → LIVE:   n ≥ probe_min_n, PASS (BH across PROBE games), real slippage ≤ 1.5× model.
Suspension (PROBE / LIVE): CUSUM alarm; rolling-100 μ < 0; slippage > 2× model;
fee share > 50 % of gross; table / regime no longer eligible.
SUSPENDED returns to SHADOW (only) after `suspend_cooldown_h`, with a fresh window.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np
import structlog
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from advantage_player.config.settings import LedgerCfg
from advantage_player.ledger.stats import (
    GameStats,
    benjamini_hochberg,
    compute_stats,
    cusum_alarm,
    passes,
)
from advantage_player.storage.models import GameRow, GameTransitionRow, LedgerStatRow, TradeRow

log = structlog.get_logger(__name__)


class GameStatus(StrEnum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PROBE = "PROBE"
    LIVE = "LIVE"
    SUSPENDED = "SUSPENDED"


ALLOWED: dict[GameStatus, set[GameStatus]] = {
    GameStatus.RESEARCH: {GameStatus.SHADOW},
    GameStatus.SHADOW: {GameStatus.PROBE},
    GameStatus.PROBE: {GameStatus.LIVE, GameStatus.SUSPENDED},
    GameStatus.LIVE: {GameStatus.SUSPENDED},
    GameStatus.SUSPENDED: {GameStatus.SHADOW},
}


class IllegalTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class Transition:
    env: str
    game_id: str
    from_status: GameStatus
    to_status: GameStatus
    reason: str


@dataclass
class GameState:
    game_id: str
    status: GameStatus
    since: datetime
    ref_mu: float | None = None
    ref_sigma: float | None = None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def baseline_of(game_id: str) -> str:
    strat, rest = game_id.split("|", 1)
    return f"BASELINE_{strat}|{rest}"


class Lifecycle:
    def __init__(self, db: Engine, env: str, cfg: LedgerCfg, cooldown_h: float = 24.0) -> None:
        self.db = db
        self.env = env
        self.cfg = cfg
        self.cooldown = timedelta(hours=cooldown_h)
        self.games: dict[str, GameState] = {}
        self.transition_hooks: list[Callable[[Transition], None]] = []
        self.load()

    # ---- state -----------------------------------------------------------------------------
    def load(self) -> None:
        with Session(self.db) as s:
            rows = s.scalars(select(GameRow).where(GameRow.env == self.env)).all()
            self.games = {
                r.game_id: GameState(
                    r.game_id, GameStatus(r.status), _aware(r.since), r.ref_mu, r.ref_sigma
                )
                for r in rows
            }

    def status(self, game_id: str) -> GameStatus:
        g = self.games.get(game_id)
        return g.status if g else GameStatus.RESEARCH

    def ensure(self, game_id: str, now: datetime) -> GameState:
        g = self.games.get(game_id)
        if g is None:
            g = self.games[game_id] = GameState(game_id, GameStatus.RESEARCH, now)
            with Session(self.db) as s:
                s.add(GameRow(env=self.env, game_id=game_id, status=g.status, since=now))
                s.commit()
        return g

    def transition(
        self,
        game_id: str,
        to: GameStatus,
        reason: str,
        now: datetime,
        ref: GameStats | None = None,
        *,
        force: bool = False,
    ) -> Transition:
        g = self.ensure(game_id, now)
        if not force and to not in ALLOWED[g.status]:
            raise IllegalTransitionError(f"{game_id}: {g.status} → {to} not allowed")
        t = Transition(self.env, game_id, g.status, to, reason)
        g.status, g.since = to, now
        if ref is not None:
            g.ref_mu, g.ref_sigma = ref.mean, ref.std
        with Session(self.db) as s:
            row = s.scalars(
                select(GameRow).where(GameRow.env == self.env, GameRow.game_id == game_id)
            ).one()
            row.status, row.since, row.ref_mu, row.ref_sigma = to, now, g.ref_mu, g.ref_sigma
            s.add(
                GameTransitionRow(
                    env=self.env,
                    game_id=game_id,
                    ts=now,
                    from_status=t.from_status,
                    to_status=to,
                    reason=reason[:256],
                )
            )
            s.commit()
        log.info("game_transition", game=game_id, frm=t.from_status, to=to, reason=reason)
        for h in self.transition_hooks:
            h(t)
        return t

    # ---- data ------------------------------------------------------------------------------
    def _trades(self, game_id: str, stage: str, since: datetime) -> list[TradeRow]:
        with Session(self.db) as s:
            return list(
                s.scalars(
                    select(TradeRow)
                    .where(
                        TradeRow.env == self.env,
                        TradeRow.game_id == game_id,
                        TradeRow.stage == stage,
                        TradeRow.entry_ts >= since,
                    )
                    .order_by(TradeRow.exit_ts)
                ).all()
            )

    def _stats(self, game_id: str, stage: str, since: datetime) -> tuple[GameStats, list[TradeRow]]:
        rows = self._trades(game_id, stage, since)
        base = self._trades(baseline_of(game_id), "BASELINE", since)
        st = compute_stats(
            [r.r_net for r in rows],
            [r.fee_r for r in rows],
            [r.r_gross for r in rows],
            [b.r_net for b in base],
            self.cfg.confidence,
            self.cfg.bootstrap_samples,
        )
        return st, rows

    def real_stats(self, game_id: str) -> tuple[float, float]:
        """(μ_low, E[R²]) from real PROBE+LIVE trades of this env — Kelly's input for LIVE."""
        with Session(self.db) as s:
            probe_since = s.scalars(
                select(GameTransitionRow.ts)
                .where(
                    GameTransitionRow.env == self.env,
                    GameTransitionRow.game_id == game_id,
                    GameTransitionRow.to_status == GameStatus.PROBE.value,
                )
                .order_by(GameTransitionRow.id.desc())
                .limit(1)
            ).first()
            if probe_since is None:
                return -math.inf, 0.0
            rows = s.scalars(
                select(TradeRow.r_net).where(
                    TradeRow.env == self.env,
                    TradeRow.game_id == game_id,
                    TradeRow.stage.in_(("PROBE", "LIVE")),
                    TradeRow.entry_ts >= probe_since,
                )
            ).all()
        st = compute_stats(list(rows), conf=self.cfg.confidence, n_boot=self.cfg.bootstrap_samples)
        return st.mu_low, st.e_r2

    # ---- evaluation ------------------------------------------------------------------------
    def apply_research(self, passed_game_ids: Iterable[str], now: datetime) -> list[Transition]:
        out = []
        for gid in passed_game_ids:
            if self.ensure(gid, now).status is GameStatus.RESEARCH:
                out.append(self.transition(gid, GameStatus.SHADOW, "research OOS pass", now))
        return out

    def evaluate(
        self,
        now: datetime,
        eligible_symbols: set[str] | None = None,
        enabled_regimes: dict[str, set[str]] | None = None,
    ) -> list[Transition]:
        """Run promotions / suspensions. `enabled_regimes`: strategy name → allowed regimes."""
        out: list[Transition] = []
        for stage, nxt, min_n in (
            (GameStatus.SHADOW, GameStatus.PROBE, self.cfg.shadow_min_n),
            (GameStatus.PROBE, GameStatus.LIVE, self.cfg.probe_min_n),
        ):
            cands = [g for g in self.games.values() if g.status is stage]
            evals = [(g, *self._stats(g.game_id, stage.value, g.since)) for g in cands]
            evals = [e for e in evals if e[1].n >= 2]
            sig = benjamini_hochberg([st.p_value for _, st, _ in evals], self.cfg.bh_q)
            for (g, st, rows), bh in zip(evals, sig, strict=True):
                ok = passes(st, bh)
                self._save_stat(g, stage.value, st, bh, ok, now)
                if st.n < min_n or not ok:
                    continue
                if stage is GameStatus.PROBE:
                    ratio = _slippage_ratio(rows)
                    if ratio > self.cfg.probe_max_slippage_ratio:
                        continue
                    reason = f"probe pass n={st.n} μ_low={st.mu_low:.3f} slip×{ratio:.2f}"
                else:
                    reason = f"shadow pass n={st.n} μ_low={st.mu_low:.3f} p={st.p_value:.4f}"
                out.append(self.transition(g.game_id, nxt, reason, now, ref=st))
        for g in [
            g for g in self.games.values() if g.status in (GameStatus.PROBE, GameStatus.LIVE)
        ]:
            why = self._suspend_reason(g, eligible_symbols, enabled_regimes)
            if why:
                out.append(self.transition(g.game_id, GameStatus.SUSPENDED, why, now))
        for g in [g for g in self.games.values() if g.status is GameStatus.SUSPENDED]:
            if now - g.since >= self.cooldown:
                out.append(self.transition(g.game_id, GameStatus.SHADOW, "cooldown over", now))
        return out

    def _suspend_reason(
        self, g: GameState, eligible: set[str] | None, regimes: dict[str, set[str]] | None
    ) -> str | None:
        strat, symbol, _tf, regime = g.game_id.split("|")
        if eligible is not None and symbol not in eligible:
            return "table no longer eligible"
        if regimes is not None and regime not in regimes.get(strat, set()):
            return "regime no longer eligible"
        st, rows = self._stats(g.game_id, g.status.value, g.since)
        r = np.array([x.r_net for x in rows])
        if (
            g.ref_mu is not None
            and g.ref_sigma
            and cusum_alarm(r, g.ref_mu, g.ref_sigma, self.cfg.cusum_k, self.cfg.cusum_h)
        ):
            return "CUSUM edge-decay alarm"
        w = self.cfg.rolling_window
        if len(r) >= w and float(r[-w:].mean()) < 0:
            return f"rolling-{w} μ < 0"
        if len(rows) >= 10 and _slippage_ratio(rows) > self.cfg.suspend_slippage_ratio:
            return "slippage > 2× model"
        if len(rows) >= 20 and st.fee_share > self.cfg.suspend_fee_share:
            return f"fee share {st.fee_share:.0%} of gross"
        return None

    def _save_stat(
        self, g: GameState, window: str, st: GameStats, bh: bool, ok: bool, now: datetime
    ) -> None:
        d = {
            k: (None if isinstance(v, float) and not math.isfinite(v) else v)
            for k, v in asdict(st).items()
        }
        with Session(self.db) as s:
            s.add(
                LedgerStatRow(
                    env=self.env,
                    ts=now,
                    game_id=g.game_id,
                    status=g.status,
                    window=window,
                    stats=d,
                    bh_significant=bh,
                    passed=ok,
                    kelly=st.kelly,
                )
            )
            s.commit()

    # ---- go-live ---------------------------------------------------------------------------
    def seed_from(self, other: Lifecycle, now: datetime) -> list[Transition]:
        """Go-live: games that reached PROBE/LIVE elsewhere start at PROBE here (never LIVE)."""
        out = []
        for g in other.games.values():
            if g.status in (GameStatus.PROBE, GameStatus.LIVE) and g.game_id not in self.games:
                self.ensure(g.game_id, now)
                out.append(
                    self.transition(
                        g.game_id,
                        GameStatus.PROBE,
                        f"seeded from {other.env} ({g.status})",
                        now,
                        force=True,
                    )
                )
        return out


def _slippage_ratio(rows: list[TradeRow]) -> float:
    real = sum(r.slippage_r for r in rows)
    model = sum(r.model_slippage_r for r in rows)
    if model <= 0:
        return 0.0 if real <= 0 else math.inf
    return real / model
