"""SPEC §7 — every discipline rule has a test that fails if the rule is violated."""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from advantage_player.config.changes import ConfigManager, diff, is_increase
from advantage_player.config.settings import AppConfig, DisciplineCfg, RiskCfg, load_config
from advantage_player.execution.executor import Executor
from advantage_player.ledger.lifecycle import GameStatus
from advantage_player.risk.caps import OpenRisk, cap_room
from advantage_player.risk.controls import Controls
from advantage_player.risk.discipline import (
    Discipline,
    DisciplineViolationError,
    bet_mode,
    entry_blockers,
    validate_sl_move,
)
from advantage_player.risk.manager import EntryRequest, decide
from advantage_player.risk.sizing import kelly_risk_fraction, size_order
from advantage_player.sim.engine import SignalEvent
from advantage_player.strategies.base import EntryType, Side, Signal, Target
from advantage_player.strategies.indicators import Regime

from .fakes import BR, SI, FakeCtx, FakeGateway

R, D = RiskCfg(), DisciplineCfg()
CFG = load_config("config.yaml")
MIN = 60_000
HOUR = 3_600_000


# ---- rule 1: no martingale ------------------------------------------------------------------
@settings(max_examples=300, deadline=None)
@given(
    bankroll=st.floats(1_000, 1e6),
    loss=st.floats(0.0001, 0.05),
    dd=st.floats(0, 0.09),
    mu_low=st.floats(0.001, 1),
    e_r2=st.floats(0.5, 10),
)
def test_rule1_size_after_loss_never_larger(
    bankroll: float, loss: float, dd: float, mu_low: float, e_r2: float
) -> None:
    """A loss lowers bankroll and raises drawdown; size can only go down, all else equal."""
    before_f = kelly_risk_fraction(mu_low, e_r2, dd, R)
    after_f = kelly_risk_fraction(mu_low, e_r2, min(dd + loss, 0.99), R)
    assert after_f <= before_f
    b = size_order(bankroll, before_f, 60_000, 59_400, SI, BR, 50, R)
    a = size_order(bankroll * (1 - loss), after_f, 60_000, 59_400, SI, BR, 50, R)
    if a is not None:
        assert b is not None and a.qty <= b.qty


def test_rule1_sizing_inputs_exclude_streaks() -> None:
    params = set(inspect.signature(kelly_risk_fraction).parameters)
    assert params == {"mu_low", "e_r2", "drawdown", "cfg"}
    params = set(inspect.signature(decide).parameters)
    assert not any("streak" in p or "last" in p or "loss_count" in p for p in params)


# ---- rule 2: no averaging down / adding / pyramiding ----------------------------------------
def test_rule2_no_second_position_on_symbol() -> None:
    held = [OpenRisk("BTCUSDT", 1, 0.001, 0)]
    for d in (1, -1):  # neither adding nor flipping into an existing symbol
        assert cap_room(held, "BTCUSDT", d, 0, R).allowed_f == 0


def _ev(
    gid: str = "A|BTCUSDT|1m|TREND",
    ts: int = 1,
    side: Side = "BUY",
    entry_type: EntryType = "market",
) -> SignalEvent:
    stop = 59_400.0 if side == "BUY" else 60_600.0
    sig = Signal(
        side,
        entry_type,
        60_000.0,
        stop,
        (Target(1.5, 0.5), Target(2.5, 0.5)),
        "t",
        True,
        1.0,
        300.0,
        30,
    )
    return SignalEvent(gid, "BTCUSDT", "1m", Regime.TREND, sig, ts, False)


async def test_rule2_executor_never_adds_to_open_symbol() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(ts=1), GameStatus.PROBE)
    n_orders = gw.kinds().count("order")
    await ex.on_signal(_ev(gid="C|BTCUSDT|1m|RANGE", ts=2), GameStatus.PROBE)
    assert gw.kinds().count("order") == n_orders


# ---- rule 3: exchange-side SL confirmed within 2 s, else flatten + alert ---------------------
async def test_rule3_sl_placed_closeposition_markprice_via_algo() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    algos = [c[1] for c in gw.calls if c[0] == "algo"]
    sl = [a for a in algos if a[1] == "STOP_MARKET"]
    assert sl and sl[0][4].get("close_position") is True
    first_order = next(i for i, c in enumerate(gw.calls) if c[0] == "order")
    first_algo = next(i for i, c in enumerate(gw.calls) if c[0] == "algo")
    assert first_algo == first_order + 1  # SL is the very next action after the fill
    assert ex.positions["BTCUSDT"].sl_id


async def test_rule3_sl_not_confirmed_in_time_flattens_and_alerts() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    gw.algo_delay_s = 2.2  # slower than the 2 s confirm window
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    await asyncio.sleep(0)
    assert ("cancel_all", "BTCUSDT") in gw.calls
    assert gw.position_amt == 0  # flattened
    assert any(k == "risk" and "FLATTEN" in t for k, t in ctx.notes)


async def test_rule3_sl_rejected_flattens() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    gw.algo_reject = {"STOP_MARKET"}
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    assert gw.position_amt == 0


async def test_rule3_orphan_without_sl_is_flattened_by_reconcile() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    gw.position_amt = 0.01
    gw.open_algo_orders = _no_algos  # type: ignore[method-assign]
    notes = await ex.reconcile()
    assert any("orphan" in n for n in notes) and gw.position_amt == 0


async def _no_algos() -> list[dict[str, Any]]:
    return []


# ---- rule 4: SL moves only toward profit ----------------------------------------------------
def test_rule4_validate_sl_move() -> None:
    validate_sl_move(1, 99.0, 100.0)
    validate_sl_move(-1, 101.0, 100.0)
    with pytest.raises(DisciplineViolationError):
        validate_sl_move(1, 99.0, 98.0)
    with pytest.raises(DisciplineViolationError):
        validate_sl_move(-1, 101.0, 102.0)


async def test_rule4_executor_refuses_widening_and_is_make_before_break() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    pos = ex.positions["BTCUSDT"]
    with pytest.raises(DisciplineViolationError):
        await ex.move_sl(pos, pos.stop - 100, "widen")
    gw.calls.clear()
    await ex.move_sl(pos, 60_000.0, "breakeven")
    kinds = [(c[0], c[1][1] if c[0] == "algo" else None) for c in gw.calls]
    # protective qty-SL first, THEN cancel old, then new closePosition SL, then drop temp
    assert kinds[0] == ("algo", "STOP_MARKET") and "qty" in gw.calls[0][1][4]
    assert kinds[1][0] == "cancel_algo"
    assert gw.calls[2][1][4].get("close_position") is True
    assert pos.stop == 60_000.0


# ---- rule 5: no revenge ---------------------------------------------------------------------
def test_rule5_symbol_lock_and_loss_streak_pause() -> None:
    d = Discipline(D)
    t = 1_000_000_000
    d.on_trade_closed("BTCUSDT", "A|BTCUSDT|1m|TREND", -1.0, "stop", t)
    assert d.entry_block("BTCUSDT", t + 14 * MIN) == "symbol_locked_after_stop"
    assert d.entry_block("BTCUSDT", t + 16 * MIN) is None
    assert d.entry_block("ETHUSDT", t + MIN) is None
    games: list[str] = []
    for i in (1, 2):  # losses #2 and #3 (the stop-out above was #1)
        games = d.on_trade_closed(f"S{i}", f"G{i}", -0.5, "time", t)
    assert games == [] and d.entry_block("X", t) is None
    games = d.on_trade_closed("S3", "G3", -0.5, "time", t)  # 4th consecutive loss
    assert games == ["A|BTCUSDT|1m|TREND", "G1", "G2", "G3"]  # edge-decay check set
    assert d.entry_block("X", t + 29 * MIN) == "paused_loss_streak"
    assert d.entry_block("X", t + 31 * MIN) is None


def test_rule5_win_resets_streak() -> None:
    d = Discipline(D)
    for _ in range(3):
        d.on_trade_closed("S", "G", -1, "time", 0)
    d.on_trade_closed("S", "G", +0.1, "target", 0)
    assert d.on_trade_closed("S", "G", -1, "time", 0) == []


# ---- rule 6: only LIVE trades normal size, only PROBE trades probe size ----------------------
@pytest.mark.parametrize("status", [GameStatus.RESEARCH, GameStatus.SHADOW, GameStatus.SUSPENDED])
def test_rule6_non_trading_statuses_never_bet(status: GameStatus) -> None:
    assert bet_mode(status) is None
    req = EntryRequest("BTCUSDT", 1, 60_000, 59_400, status, 0, 0.5, 1.0)
    dec = decide(
        req,
        CFG,
        Discipline(D),
        now_ms=0,
        bankroll=10_000,
        drawdown=0,
        daily_loss_frac=0,
        open_risks=[],
        blockers=[],
        si=SI,
        brackets=BR,
    )
    assert not dec.bet and dec.reason == f"status_{status.value}"


def test_rule6_probe_uses_probe_size_live_uses_kelly() -> None:
    kw: dict[str, Any] = dict(
        now_ms=0,
        bankroll=100_000,
        drawdown=0,
        daily_loss_frac=0,
        open_risks=[],
        blockers=[],
        si=SI,
        brackets=BR,
    )
    probe = decide(
        EntryRequest("BTCUSDT", 1, 60_000, 59_400, GameStatus.PROBE, 0, 0.5, 1.0),
        CFG,
        Discipline(D),
        **kw,
    )
    assert probe.bet and probe.mode == "probe" and probe.f <= CFG.risk.probe_risk + 1e-12
    live = decide(
        EntryRequest("BTCUSDT", 1, 60_000, 59_400, GameStatus.LIVE, 0, 0.02, 2.0),
        CFG,
        Discipline(D),
        **kw,
    )
    assert live.bet and live.mode == "live"
    assert live.f == pytest.approx(0.25 * 0.02 / 2.0, rel=0.05)
    none = decide(
        EntryRequest("BTCUSDT", 1, 60_000, 59_400, GameStatus.LIVE, 0, -0.1, 2.0),
        CFG,
        Discipline(D),
        **kw,
    )
    assert not none.bet and none.reason == "kelly_zero"


# ---- rule 7: hard kill at 10 % drawdown ------------------------------------------------------
async def test_rule7_kill_cancels_flattens_halts() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    await ex.kill("drawdown 10.2%")
    assert ("cancel_all", "BTCUSDT") in gw.calls
    assert gw.position_amt == 0
    assert ctx.discipline.state.halted
    req = EntryRequest("ETHUSDT", 1, 60_000, 59_400, GameStatus.LIVE, 0, 0.5, 1.0)
    dec = decide(
        req,
        CFG,
        ctx.discipline,
        now_ms=ctx.now_ms(),
        bankroll=10_000,
        drawdown=0,
        daily_loss_frac=0,
        open_risks=[],
        blockers=[],
        si=SI,
        brackets=BR,
    )
    assert dec.reason == "halted_kill"


def test_rule7_resume_only_manually_after_12h() -> None:
    d = Discipline(D)
    t = 5 * HOUR
    d.kill(t)
    assert d.resume(t + 11 * HOUR, 0.0, 0.10).startswith("refused")
    assert d.state.halted
    assert d.resume(t + 13 * HOUR, 0.12, 0.10).startswith("refused")  # still ≥ 10 % down
    assert d.resume(t + 13 * HOUR, 0.02, 0.10) == "resumed"
    assert not d.state.halted


def test_rule7_drawdown_halt_blocks_entries() -> None:
    req = EntryRequest("BTCUSDT", 1, 60_000, 59_400, GameStatus.LIVE, 0, 0.5, 1.0)
    dec = decide(
        req,
        CFG,
        Discipline(D),
        now_ms=0,
        bankroll=10_000,
        drawdown=0.10,
        daily_loss_frac=0,
        open_risks=[],
        blockers=[],
        si=SI,
        brackets=BR,
    )
    assert dec.reason == "drawdown_halt"


# ---- rule 8: config pre-commitment -----------------------------------------------------------
def _with(**risk: float) -> AppConfig:
    return CFG.model_copy(update={"risk": CFG.risk.model_copy(update=risk)})


def test_rule8_increase_queued_24h_decrease_immediate() -> None:
    mgr = ConfigManager(CFG)
    ev = mgr.propose(_with(max_risk_per_trade=0.02, kelly_fraction=0.1), 0, 0.0)
    kinds = {e.kind: e.paths for e in ev}
    assert kinds["applied"] == {"risk.kelly_fraction": 0.1}  # decrease: now
    assert kinds["queued"] == {"risk.max_risk_per_trade": 0.02}  # increase: later
    assert mgr.effective.risk.max_risk_per_trade == 0.01
    assert mgr.tick(23 * HOUR, 0.0) == []
    done = mgr.tick(24 * HOUR, 0.0)
    assert done[0].kind == "applied" and mgr.effective.risk.max_risk_per_trade == 0.02


def test_rule8_increase_rejected_while_drawdown_over_5pct() -> None:
    mgr = ConfigManager(CFG)
    ev = mgr.propose(_with(max_positions=6), 0, 0.051)
    assert [e.kind for e in ev] == ["rejected"]
    mgr2 = ConfigManager(CFG)
    mgr2.propose(_with(max_positions=6), 0, 0.0)
    assert mgr2.tick(25 * HOUR, 0.06)[0].kind == "rejected"  # re-checked when due
    assert mgr2.effective.risk.max_positions == 4


def test_rule8_classification_and_versioning() -> None:
    assert is_increase("target_leverage", 50, 75)
    assert not is_increase("target_leverage", 50, 20)
    assert is_increase("games.A_trend_pullback.enabled", False, True)
    assert is_increase("games.A_trend_pullback.timeframes", ["1m"], ["1m", "15s"])
    assert not is_increase("games.A_trend_pullback.timeframes", ["1m", "15s"], ["1m"])
    assert is_increase("universe.min_quote_volume_usd", 5e7, 1e7)  # enabling tables
    assert is_increase("games.A_trend_pullback.zone_atr", 0.3, 0.31)  # unknown ⇒ increase
    assert is_increase("discipline.loss_streak_pause_n", 4, 5)
    assert not is_increase("discipline.symbol_lock_after_stop_min", 15, 30)
    d = diff(CFG, _with(daily_loss_limit=0.02))
    assert d.decreases == {"risk.daily_loss_limit": 0.02} and not d.increases
    assert _with(daily_loss_limit=0.02).hash != CFG.hash


def test_rule8_reverting_file_cancels_pending() -> None:
    mgr = ConfigManager(CFG)
    mgr.propose(_with(max_positions=6), 0, 0.0)
    ev = mgr.propose(CFG, HOUR, 0.0)
    assert [e.kind for e in ev] == ["cancelled"] and not mgr.pending


# ---- rule 9: controls can only reduce risk ---------------------------------------------------
class _Target:
    def __init__(self) -> None:
        self.cfg = CFG
        self.risk_scale = 1.0
        self.overrides: dict[str, Any] = {}
        self.applied: list[AppConfig] = []

    def pause(self) -> None: ...
    def resume(self) -> str:
        return "resumed"

    async def close(self, symbol: str | None) -> str:
        return "ok"

    async def kill(self, reason: str) -> None: ...

    def apply_effective(self, cfg: AppConfig, note: str) -> None:
        self.cfg = cfg
        self.applied.append(cfg)


async def test_rule9_controls_only_lower_risk() -> None:
    t = _Target()
    c = Controls(t, lambda m: None)
    assert (await c.set_risk_scale(1.5)).startswith("refused")
    assert (await c.set_risk_scale(0.5)) == "risk scale set to 0.5"
    assert (await c.set_risk_scale(0.8)).startswith("refused")  # can't go back up
    assert (await c.lower("risk.max_risk_per_trade", 0.02)).startswith("refused")
    assert (await c.lower("target_leverage", 75)).startswith("refused")
    assert (await c.lower("risk.max_risk_per_trade", 0.005)).endswith("0.005")
    assert t.cfg.risk.max_risk_per_trade == 0.005
    assert t.overrides == {"risk.max_risk_per_trade": 0.005}


def test_rule9_no_risk_increasing_command_exists() -> None:
    public = {
        n for n, _ in inspect.getmembers(Controls, inspect.isfunction) if not n.startswith("_")
    }
    assert public == {"pause", "resume", "close", "kill", "set_risk_scale", "lower", "view"}


# ---- rule 10: entry blockers -----------------------------------------------------------------
def _blk(**kw: Any) -> list[str]:
    base: dict[str, Any] = dict(
        now_ms=10 * HOUR,
        stale=[],
        ws_healthy=True,
        spread_bps=1.0,
        regime="TREND",
        next_funding_ms=16 * HOUR,
        last_funding_ms=8 * HOUR,
        clock_drift_ms=50,
        rate_headroom=0.9,
    )
    base.update(kw)
    return entry_blockers(D, **base)


def test_rule10_each_entry_blocker() -> None:
    assert _blk() == []
    assert _blk(stale=["book"]) == ["stale_data:book"]
    assert _blk(ws_healthy=False) == ["ws_down"]
    assert _blk(spread_bps=6.0) == ["spread"]
    assert _blk(spread_bps=None) == ["spread"]
    assert _blk(regime="HIGH_VOL") == ["high_vol"]
    assert _blk(next_funding_ms=10 * HOUR + 4 * MIN) == ["funding_window"]
    assert _blk(last_funding_ms=10 * HOUR - 4 * MIN) == ["funding_window"]
    assert _blk(clock_drift_ms=501) == ["clock_drift"]
    assert _blk(rate_headroom=0.1) == ["rate_limit_headroom"]


async def test_rule10_blocked_signal_is_recorded_as_no_bet() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ctx.block = ["ws_down"]
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    assert "order" not in gw.kinds()
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from advantage_player.storage.models import SignalRow

    with Session(ctx.journal.db) as s:
        row = s.scalars(select(SignalRow)).one()
        assert (row.decision, row.reason) == ("NO_BET", "ws_down")


# ---- rule 11: graceful shutdown keeps SLs ----------------------------------------------------
async def test_rule11_stop_cancels_entries_but_keeps_sl() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    gw.calls.clear()
    await ex.stop()
    assert "cancel_algo" not in gw.kinds() and "cancel_all" not in gw.kinds()
    assert ex.positions["BTCUSDT"].sl_id  # still protected, state persisted


async def test_restart_safe_state_reload_and_no_double_entry() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(ts=7), GameStatus.PROBE)
    ex2 = Executor(ctx, gw, "demo")  # type: ignore[arg-type]  # "restart"
    assert "BTCUSDT" in ex2.positions and ex2.positions["BTCUSDT"].state == "open"
    n = gw.kinds().count("order")
    await ex2.on_signal(_ev(ts=7), GameStatus.PROBE)  # same signal replayed
    assert gw.kinds().count("order") == n


async def test_finalize_records_probe_trade_r_and_locks_symbol() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    pos = ex.positions["BTCUSDT"]
    q = pos.filled_qty
    gw.user_trades = [
        {
            "side": "BUY",
            "price": "60000",
            "qty": str(q),
            "commission": "0.5",
            "realizedPnl": "0",
            "time": pos.signal_ms + 1,
        },
        {
            "side": "SELL",
            "price": "59400",
            "qty": str(q),
            "commission": "0.5",
            "realizedPnl": str(-600 * q),
            "time": pos.signal_ms + 2,
        },
    ]
    gw.position_amt = 0
    await ex._finalize(pos)
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from advantage_player.storage.models import TradeRow

    with Session(ctx.journal.db) as s:
        t = s.scalars(select(TradeRow)).one()
    risk = q * 600
    assert t.stage == "PROBE"
    assert t.r_net == pytest.approx((-600 * q - 1.0 - 0.5) / risk)
    assert t.exit_reason == "stop"
    assert ctx.discipline.entry_block("BTCUSDT", ctx.now_ms() + MIN) == "symbol_locked_after_stop"
    assert any(k == "close" for k, _ in ctx.notes)
    assert "BTCUSDT" not in ex.positions


async def test_maker_entry_timeout_without_valid_taker_fallback_abandons() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ctx.cfg = ctx.cfg.model_copy(
        update={"execution": ctx.cfg.execution.model_copy(update={"maker_timeout_s": 0.05})}
    )
    from advantage_player.marketdata.events import BookTicker

    ctx.bt = BookTicker("BTCUSDT", 2, 60_100, 1, 60_100.1, 1, 0)  # ran away > 3 bps
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(entry_type="maker"), GameStatus.PROBE)
    orders = [c[1] for c in gw.calls if c[0] == "order"]
    assert len(orders) == 1 and orders[0][1] == "LIMIT" and orders[0][4]["tif"] == "GTX"
    assert "BTCUSDT" not in ex.positions
    assert Decimal(0) == 0


async def test_rule4_intentional_sl_cancel_is_not_treated_as_lost_sl() -> None:
    ctx, gw = FakeCtx(), FakeGateway()
    ex = Executor(ctx, gw, "demo")  # type: ignore[arg-type]
    await ex.on_signal(_ev(), GameStatus.PROBE)
    pos = ex.positions["BTCUSDT"]
    old = pos.sl_id
    await ex.move_sl(pos, 59_700.0, "trail")
    gw.calls.clear()
    await ex.on_user_event(
        {"e": "ALGO_UPDATE", "o": {"s": "BTCUSDT", "caid": old, "X": "CANCELED"}}
    )
    assert gw.calls == []  # no restore, no flatten
    await ex.on_user_event(
        {"e": "ALGO_UPDATE", "o": {"s": "BTCUSDT", "caid": pos.sl_id, "X": "EXPIRED"}}
    )
    assert [c[0] for c in gw.calls] == ["algo"]  # unexpected loss of the live SL → re-placed
