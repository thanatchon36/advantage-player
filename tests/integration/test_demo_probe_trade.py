"""DEMO-only end-to-end PROBE trade: entry → SL/TP via algoOrder → SL move → close →
ledger updated → notification sent. Skipped when DEMO keys are absent. Never runs on LIVE."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from advantage_player.config.settings import Secrets, load_config
from advantage_player.exchange.endpoints import ENDPOINTS
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.rest import BinanceRest
from advantage_player.execution.executor import Executor
from advantage_player.execution.gateway import Gateway
from advantage_player.execution.userstream import UserStream
from advantage_player.ledger.journal import Journal
from advantage_player.ledger.lifecycle import GameStatus
from advantage_player.marketdata.events import BookTicker
from advantage_player.risk.discipline import Discipline
from advantage_player.sim.engine import SignalEvent
from advantage_player.sim.fills import Costs
from advantage_player.storage.db import make_engine
from advantage_player.storage.models import SignalRow, TradeRow
from advantage_player.strategies.base import Signal, Target
from advantage_player.strategies.indicators import Regime

pytestmark = pytest.mark.integration
SYM = "BTCUSDT"


class Ctx:
    def __init__(self, rest: BinanceRest, meta: ExchangeMetadata, db_path: Path) -> None:
        self.cfg = load_config("config.yaml")
        self.meta = meta
        self.rest = rest
        self.journal = Journal(make_engine(f"sqlite:///{db_path}"), "demo")
        self.discipline = Discipline(self.cfg.discipline)
        self.risk_scale = 1.0
        self.notes: list[tuple[str, str]] = []
        self.equity = 0.0
        self.bt: BookTicker | None = None
        self.mark_px: float | None = None

    def now_ms(self) -> int:
        return self.rest.clock.now_ms()

    def bankroll(self) -> float:
        return self.equity

    def drawdown(self) -> float:
        return 0.0

    def daily_loss(self) -> float:
        return 0.0

    def blockers(self, symbol: str, regime: str) -> list[str]:
        return []

    def cluster(self, symbol: str) -> int:
        return 0

    def real_stats(self, game_id: str) -> tuple[float, float]:
        return float("-inf"), 0.0

    def book(self, symbol: str) -> BookTicker | None:
        return self.bt

    def mark(self, symbol: str) -> float | None:
        return self.mark_px

    def costs(self, symbol: str) -> Costs:
        return Costs()

    async def notify(self, kind: str, text: str) -> None:
        self.notes.append((kind, text))

    async def edge_check(self, game_ids: list[str]) -> None: ...

    def persist_discipline(self) -> None: ...

    async def refresh_book(self) -> None:
        b = await self.rest.public("GET", "/fapi/v1/ticker/bookTicker", {"symbol": SYM})
        self.bt = BookTicker(
            SYM,
            int(b["lastUpdateId"]),
            float(b["bidPrice"]),
            1.0,
            float(b["askPrice"]),
            1.0,
            int(b["time"]),
        )
        m = await self.rest.public("GET", "/fapi/v1/premiumIndex", {"symbol": SYM})
        self.mark_px = float(m["markPrice"])


async def _algos(gw: Gateway) -> list[dict[str, Any]]:
    return [a for a in await gw.open_algo_orders() if a["symbol"] == SYM]


async def test_demo_probe_trade_end_to_end(demo_secrets: Secrets, tmp_path: Path) -> None:
    ep = ENDPOINTS[demo_secrets.binance_env]
    assert "demo" in ep.rest  # never LIVE
    rest = BinanceRest(ep.rest, demo_secrets.api_key, demo_secrets.api_secret)
    gw = Gateway(rest)
    uds: UserStream | None = None
    try:
        await rest.sync_time()
        if any(p["symbol"] == SYM for p in await gw.positions()):
            pytest.skip("a BTCUSDT position already exists on the DEMO account")
        meta = ExchangeMetadata(rest)
        await meta.refresh_exchange_info()
        await meta.refresh_brackets()
        ctx = Ctx(rest, meta, tmp_path / "t.db")
        ctx.equity = float((await gw.account())["totalMarginBalance"])
        await ctx.refresh_book()
        assert ctx.bt is not None
        ex = Executor(ctx, gw, "demo")
        uds = UserStream(rest, ep.ws_stream, ex.on_user_event)
        await uds.start()
        await asyncio.sleep(1)

        entry = ctx.bt.ask
        stop = round(entry * 0.99, 1)
        sig = Signal(
            "BUY",
            "market",
            entry,
            stop,
            (Target(1.5, 0.5), Target(2.5, 0.5)),
            "itest",
            True,
            0.0,
            0.0,
            0,
        )
        ev = SignalEvent(f"A|{SYM}|1m|TREND", SYM, "1m", Regime.TREND, sig, ctx.now_ms(), False)
        await ex.on_signal(ev, GameStatus.PROBE)

        pos = ex.positions.get(SYM)
        assert pos is not None and pos.state == "open", ctx.notes
        algos = await _algos(gw)
        sl = [a for a in algos if a["orderType"] == "STOP_MARKET"]
        tp = [a for a in algos if a["orderType"] == "TAKE_PROFIT_MARKET"]
        assert len(sl) == 1 and str(sl[0]["closePosition"]).lower() == "true"
        assert sl[0]["workingType"] == "MARK_PRICE"
        assert len(tp) == 2
        assert pos.f <= ctx.cfg.risk.max_risk_per_trade

        # make-before-break SL move toward profit (verifies the -4130 workaround on DEMO)
        new_stop = round(stop * 1.002, 1)
        assert await ex.move_sl(pos, new_stop, "itest")
        sl = [a for a in await _algos(gw) if a["orderType"] == "STOP_MARKET"]
        assert len(sl) == 1 and float(sl[0]["triggerPrice"]) == pytest.approx(new_stop)

        await ctx.refresh_book()
        await ex.close(SYM, "manual")
        for _ in range(40):
            await asyncio.sleep(0.5)
            if SYM not in ex.positions:
                break
        if SYM in ex.positions:  # UDS missed it → reconcile path must finalize
            await ex.reconcile()
        assert SYM not in ex.positions
        assert await _algos(gw) == []  # leftover TP/SL cleaned up

        with Session(ctx.journal.db) as s:
            t = s.scalars(select(TradeRow)).one()
            sigs = s.scalars(select(SignalRow)).all()
        assert t.stage == "PROBE" and t.qty > 0 and t.fees_usd > 0
        assert t.exit_reason == "manual"
        assert [x.decision for x in sigs] == ["BET"]
        kinds = [k for k, _ in ctx.notes]
        assert "open" in kinds and "close" in kinds
    finally:
        if uds:
            await uds.stop()
        for p in await gw.positions():  # safety net: never leave a DEMO position behind
            if p["symbol"] == SYM:
                await gw.cancel_all(SYM)
                amt = float(p["positionAmt"])
                await rest.signed(
                    "POST",
                    "/fapi/v1/order",
                    {
                        "symbol": SYM,
                        "side": "SELL" if amt > 0 else "BUY",
                        "type": "MARKET",
                        "quantity": str(abs(amt)),
                        "reduceOnly": "true",
                    },
                )
        await gw.cancel_all(SYM)
        await rest.aclose()
