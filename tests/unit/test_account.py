from __future__ import annotations

import pytest

from advantage_player.config.settings import RiskCfg
from advantage_player.execution.account import AccountTracker
from advantage_player.storage.db import make_engine

DAY = 86_400_000
T0 = 1_791_676_800_000  # 2026-10-11 00:00 UTC


def _tracker() -> tuple[AccountTracker, list[tuple[str, str]], list[str]]:
    alerts: list[tuple[str, str]] = []
    halts: list[str] = []

    async def alert(kind: str, text: str) -> None:
        alerts.append((kind, text))

    t = AccountTracker(make_engine("sqlite:///:memory:"), "demo", RiskCfg(), alert)

    async def on_halt(why: str) -> None:
        halts.append(why)

    t.on_halt = on_halt
    return t, alerts, halts


async def test_drawdown_steps_and_hard_kill_trigger() -> None:
    t, alerts, halts = _tracker()
    await t.update(10_000, T0)
    await t.update(9_600, T0 + 1000)  # −4 %
    assert t.snapshot(T0).dd_step == 1.0 and not alerts
    await t.update(9_400, T0 + 2000)  # −6 % → ×0.5
    assert t.snapshot(T0).dd_step == 0.5 and "multiplier 0.5" in alerts[-1][1]
    await t.update(8_990, T0 + 3000)  # −10.1 % → HALT
    assert halts and "HARD KILL" in alerts[-1][1]


async def test_daily_loss_uses_start_of_day_equity() -> None:
    t, _, _ = _tracker()
    await t.update(10_000, T0 + 1000)
    await t.update(9_700, T0 + 2000)
    assert t.snapshot(T0 + 2000).daily_loss == pytest.approx(0.03)
    await t.update(9_700, T0 + DAY + 1)  # new UTC day resets the baseline
    assert t.snapshot(T0 + DAY + 1).daily_loss == 0.0


async def test_transfer_out_is_not_a_drawdown_and_resets_sweep_baseline() -> None:
    t, _, halts = _tracker()
    await t.update(10_000, T0)
    n = t.apply_transfers([{"tranId": 1, "income": "-2000", "time": T0 + 10}])
    assert n == 1 and t.apply_transfers([{"tranId": 1, "income": "-2000", "time": T0 + 10}]) == 0
    await t.update(8_000, T0 + 20)
    assert t.snapshot(T0 + 20).drawdown == pytest.approx(0.0)
    assert not halts and t.sweep_baseline == 8_000


async def test_sweep_suggestion_once_per_day() -> None:
    t, alerts, _ = _tracker()
    await t.update(10_000, T0)
    await t.update(12_100, T0 + 1000)
    await t.update(12_200, T0 + 2000)
    sweeps = [a for a in alerts if a[0] == "sweep"]
    assert len(sweeps) == 1 and "2,100.00" in sweeps[0][1]


async def test_peak_survives_restart() -> None:
    db = make_engine("sqlite:///:memory:")

    async def noop(kind: str, text: str) -> None: ...

    a = AccountTracker(db, "demo", RiskCfg(), noop)
    await a.update(10_000, T0)
    b = AccountTracker(db, "demo", RiskCfg(), noop)
    await b.update(9_000, T0 + 1)
    assert b.snapshot(T0 + 1).drawdown == pytest.approx(0.10)  # restart can't erase the peak
