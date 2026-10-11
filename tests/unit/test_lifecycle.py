from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from advantage_player.config.settings import LedgerCfg
from advantage_player.ledger.journal import Journal
from advantage_player.ledger.lifecycle import (
    GameStatus,
    IllegalTransitionError,
    Lifecycle,
    Transition,
)
from advantage_player.sim.fills import Leg, TradeResult
from advantage_player.storage.db import make_engine
from advantage_player.storage.models import GameTransitionRow

T0 = datetime(2026, 10, 1, tzinfo=UTC)
G = "A|BTCUSDT|1m|TREND"
CFG = LedgerCfg(bootstrap_samples=500)


def _db() -> Engine:
    return make_engine("sqlite:///:memory:")


def _add(
    j: Journal,
    gid: str,
    stage: str,
    rs: list[float],
    start: datetime,
    slip: float = 0.01,
    model: float = 0.01,
    fee: float = 0.05,
) -> None:
    for i, r in enumerate(rs):
        ms = int((start + timedelta(minutes=i + 1)).timestamp() * 1000)
        tr = TradeResult(
            gid,
            "BTCUSDT",
            "BUY",
            "t",
            ms,
            ms,
            ms + 1,
            100,
            100,
            99,
            [Leg(100, 100, 1, 0, "x")],
            r,
            r + fee,
            fee,
            0,
            slip,
            0,
            0,
            "target",
            False,
            None,
        )
        j.trade(tr, stage, model_slippage_r=model)


def _promote_to(lc: Lifecycle, gid: str, target: GameStatus, at: datetime) -> None:
    path = [GameStatus.SHADOW, GameStatus.PROBE, GameStatus.LIVE]
    for st in path[: path.index(target) + 1]:
        lc.transition(gid, st, "test", at)


def test_transitions_allowed_and_illegal() -> None:
    lc = Lifecycle(_db(), "demo", CFG)
    assert lc.status(G) is GameStatus.RESEARCH
    with pytest.raises(IllegalTransitionError):
        lc.transition(G, GameStatus.LIVE, "skip", T0)
    with pytest.raises(IllegalTransitionError):
        lc.transition(G, GameStatus.PROBE, "skip", T0)
    lc.transition(G, GameStatus.SHADOW, "ok", T0)
    lc.transition(G, GameStatus.PROBE, "ok", T0)
    lc.transition(G, GameStatus.SUSPENDED, "ok", T0)
    with pytest.raises(IllegalTransitionError):
        lc.transition(G, GameStatus.PROBE, "suspended may only return to SHADOW", T0)
    with pytest.raises(IllegalTransitionError):
        lc.transition(G, GameStatus.LIVE, "nope", T0)
    lc.transition(G, GameStatus.SHADOW, "ok", T0)


def test_transitions_persisted_logged_and_hooked() -> None:
    db = _db()
    lc = Lifecycle(db, "demo", CFG)
    seen: list[Transition] = []
    lc.transition_hooks.append(seen.append)
    lc.transition(G, GameStatus.SHADOW, "research pass", T0)
    assert seen[0].to_status is GameStatus.SHADOW
    again = Lifecycle(db, "demo", CFG)
    assert again.status(G) is GameStatus.SHADOW
    with Session(db) as s:
        rows = s.scalars(select(GameTransitionRow)).all()
        assert [(r.from_status, r.to_status) for r in rows] == [("RESEARCH", "SHADOW")]


def test_demo_and_live_are_separate() -> None:
    db = _db()
    demo = Lifecycle(db, "demo", CFG)
    _promote_to(demo, G, GameStatus.LIVE, T0)
    live = Lifecycle(db, "live", CFG)
    assert live.status(G) is GameStatus.RESEARCH  # demo results never unlock live
    out = live.seed_from(demo, T0)
    assert [t.to_status for t in out] == [GameStatus.PROBE]  # go-live: back at PROBE
    assert live.status(G) is GameStatus.PROBE


def test_shadow_to_probe_needs_n_and_pass() -> None:
    db = _db()
    lc, j = Lifecycle(db, "demo", CFG), Journal(db, "demo")
    lc.transition(G, GameStatus.SHADOW, "t", T0)
    rng = np.random.default_rng(0)
    good = list(rng.normal(0.5, 1.0, 199))
    _add(j, G, "SHADOW", good, T0)
    _add(j, "BASELINE_A|BTCUSDT|1m|TREND", "BASELINE", list(rng.normal(-0.05, 1, 300)), T0)
    assert lc.evaluate(T0 + timedelta(days=1)) == []  # n = 199 < 200
    _add(j, G, "SHADOW", [0.5], T0 + timedelta(hours=5))
    out = lc.evaluate(T0 + timedelta(days=1))
    assert [(t.game_id, t.to_status) for t in out] == [(G, GameStatus.PROBE)]


def test_shadow_trades_before_entering_stage_do_not_count() -> None:
    db = _db()
    lc, j = Lifecycle(db, "demo", CFG), Journal(db, "demo")
    rng = np.random.default_rng(1)
    _add(j, G, "SHADOW", list(rng.normal(0.5, 1.0, 300)), T0 - timedelta(days=2))
    lc.transition(G, GameStatus.SHADOW, "t", T0)
    assert lc.evaluate(T0 + timedelta(days=1)) == []


def test_null_game_does_not_promote_and_bh_applies() -> None:
    db = _db()
    lc, j = Lifecycle(db, "demo", CFG), Journal(db, "demo")
    rng = np.random.default_rng(2)
    for k in range(10):
        gid = f"C|S{k}USDT|1m|RANGE"
        lc.transition(gid, GameStatus.SHADOW, "t", T0)
        _add(j, gid, "SHADOW", list(rng.normal(0.0, 1.0, 250)), T0)
        _add(j, f"BASELINE_{gid}", "BASELINE", list(rng.normal(0.0, 1.0, 250)), T0)
    out = lc.evaluate(T0 + timedelta(days=1))
    assert out == []


def test_probe_to_live_requires_slippage_within_model() -> None:
    db = _db()
    lc, j = Lifecycle(db, "demo", CFG), Journal(db, "demo")
    _promote_to(lc, G, GameStatus.PROBE, T0)
    rng = np.random.default_rng(3)
    _add(j, "BASELINE_A|BTCUSDT|1m|TREND", "BASELINE", list(rng.normal(-0.05, 1, 300)), T0)
    _add(j, G, "PROBE", list(rng.normal(0.6, 1.0, 120)), T0, slip=0.02, model=0.01)  # 2×
    lc.evaluate(T0 + timedelta(days=1), None, regimes_ok())
    assert lc.status(G) is GameStatus.PROBE  # real slippage 2× model blocks LIVE
    db2 = _db()
    lc2, j2 = Lifecycle(db2, "demo", CFG), Journal(db2, "demo")
    _promote_to(lc2, G, GameStatus.PROBE, T0)
    _add(j2, "BASELINE_A|BTCUSDT|1m|TREND", "BASELINE", list(rng.normal(-0.05, 1, 300)), T0)
    _add(j2, G, "PROBE", list(rng.normal(0.6, 1.0, 120)), T0, slip=0.012, model=0.01)
    out = lc2.evaluate(T0 + timedelta(days=1), None, regimes_ok())
    assert [t.to_status for t in out] == [GameStatus.LIVE]


def regimes_ok() -> dict[str, set[str]]:
    return {"A": {"TREND"}, "B": {"TREND", "RANGE"}, "C": {"RANGE"}}


@pytest.mark.parametrize(
    ("kwargs", "eligible", "regimes", "reason"),
    [
        ({}, {"ETHUSDT"}, None, "table no longer eligible"),
        ({}, None, {"A": {"RANGE"}}, "regime no longer eligible"),
        ({"rs": [-0.2] * 100}, None, None, "rolling-100"),
        ({"slip": 0.05, "model": 0.01}, None, None, "slippage"),
        ({"fee": 2.0}, None, None, "fee share"),
    ],
)
def test_live_suspension_rules(
    kwargs: dict[str, object],
    eligible: set[str] | None,
    regimes: dict[str, set[str]] | None,
    reason: str,
) -> None:
    db = _db()
    lc, j = Lifecycle(db, "demo", CFG), Journal(db, "demo")
    _promote_to(lc, G, GameStatus.LIVE, T0)
    rs = kwargs.pop("rs", [0.3, -0.1] * 15)
    _add(j, G, "LIVE", rs, T0, **kwargs)  # type: ignore[arg-type]
    out = lc.evaluate(T0 + timedelta(hours=1), eligible, regimes)
    sus = [t for t in out if t.to_status is GameStatus.SUSPENDED]
    assert len(sus) == 1 and reason in sus[0].reason


def test_cusum_suspends_on_edge_decay() -> None:
    db = _db()
    lc, j = Lifecycle(db, "demo", CFG), Journal(db, "demo")
    _promote_to(lc, G, GameStatus.PROBE, T0)
    g = lc.games[G]
    g.ref_mu, g.ref_sigma = 0.4, 1.0
    _add(j, G, "PROBE", [-1.0] * 12, T0)
    out = lc.evaluate(T0 + timedelta(hours=1))
    assert out and "CUSUM" in out[-1].reason


def test_suspended_returns_to_shadow_after_cooldown_with_fresh_window() -> None:
    db = _db()
    lc = Lifecycle(db, "demo", CFG, cooldown_h=24)
    _promote_to(lc, G, GameStatus.LIVE, T0)
    lc.transition(G, GameStatus.SUSPENDED, "x", T0)
    assert lc.evaluate(T0 + timedelta(hours=23)) == []
    out = lc.evaluate(T0 + timedelta(hours=25))
    assert [t.to_status for t in out] == [GameStatus.SHADOW]
    assert lc.games[G].since == T0 + timedelta(hours=25)


def test_research_pass_moves_only_research_games() -> None:
    lc = Lifecycle(_db(), "demo", CFG)
    lc.transition("B|X|5s|RANGE", GameStatus.SHADOW, "t", T0)
    out = lc.apply_research([G, "B|X|5s|RANGE"], T0)
    assert [t.game_id for t in out] == [G]
