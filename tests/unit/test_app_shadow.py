from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from advantage_player.app import Bot
from advantage_player.config.settings import Secrets, StorageCfg, load_config
from advantage_player.ledger.lifecycle import GameStatus
from advantage_player.sim.engine import SignalEvent
from advantage_player.sim.fills import Leg, TradeResult
from advantage_player.storage.models import SignalRow, TradeRow
from advantage_player.strategies.base import Signal, Target
from advantage_player.strategies.indicators import Regime


def _bot(tmp_path: Path) -> Bot:
    cfg = load_config("config.yaml").model_copy(
        update={"storage": StorageCfg(url=f"sqlite:///{tmp_path}/t.db", data_dir=str(tmp_path))}
    )
    return Bot(Secrets(_env_file=None), cfg)


def _sig_ev(gid: str, baseline: bool = False, ts: int = 1000) -> SignalEvent:
    sig = Signal("BUY", "market", 100, 99, (Target(1, 1),), "t")
    return SignalEvent(gid, "BTCUSDT", "1m", Regime.TREND, sig, ts, baseline)


def _res(gid: str, ts: int = 1000) -> TradeResult:
    return TradeResult(
        gid,
        "BTCUSDT",
        "BUY",
        "t",
        ts,
        ts,
        ts + 1,
        100,
        100,
        99,
        [Leg(101, 101, 1, 0, "target")],
        1,
        1,
        0,
        0,
        0,
        0,
        1,
        "target",
        False,
        None,
    )


def test_shadow_signal_recorded_as_no_bet_and_trade_staged(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    g = "A|BTCUSDT|1m|TREND"
    bot._on_signal(_sig_ev(g))
    bot._on_result(_res(g))
    bot._on_signal(_sig_ev("BASELINE_" + g, baseline=True, ts=2000))
    bot._on_result(_res("BASELINE_" + g, ts=2000))
    with Session(bot.db) as s:
        sigs = s.scalars(select(SignalRow)).all()
        assert [(x.decision, x.reason, x.status) for x in sigs] == [
            ("NO_BET", "status_RESEARCH", "RESEARCH")
        ]
        trades = s.scalars(select(TradeRow).order_by(TradeRow.id)).all()
        assert [t.stage for t in trades] == ["RESEARCH", "BASELINE"]
        assert all(t.env == "demo" for t in trades)


async def test_probe_games_go_to_executor_and_sim_trade_is_not_probe(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    g = "A|BTCUSDT|1m|TREND"
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    bot.lifecycle.transition(g, GameStatus.SHADOW, "t", now)
    bot.lifecycle.transition(g, GameStatus.PROBE, "t", now)
    calls: list[Any] = []

    class Exec:
        async def on_signal(self, ev: SignalEvent, status: GameStatus) -> None:
            calls.append((ev.game_id, status))

        async def start(self) -> None: ...
        async def stop(self) -> None: ...

    bot.executor = Exec()
    bot._on_signal(_sig_ev(g))
    await asyncio.sleep(0)
    assert calls == [(g, GameStatus.PROBE)]
    bot._on_result(_res(g))
    with Session(bot.db) as s:
        assert s.scalars(select(TradeRow.stage)).all() == ["SIM_PROBE"]  # never counts as PROBE
    await bot.rest.aclose()
