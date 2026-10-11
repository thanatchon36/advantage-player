"""In-memory stand-ins for the exchange gateway and executor context."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from advantage_player.config.settings import AppConfig, load_config
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.models import LeverageBrackets, SymbolInfo
from advantage_player.exchange.rest import BinanceRest
from advantage_player.execution.gateway import OrderRejectedError
from advantage_player.ledger.journal import Journal
from advantage_player.marketdata.events import BookTicker
from advantage_player.risk.discipline import Discipline
from advantage_player.sim.fills import Costs
from advantage_player.storage.db import make_engine

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
                "notionalCap": 1e7,
                "notionalFloor": 0,
                "maintMarginRatio": 0.004,
                "cum": 0,
            }
        ],
    }
)


class FakeRest:
    def __init__(self, gw: FakeGateway) -> None:
        self.gw = gw

    async def signed(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if path == "/fapi/v1/userTrades":
            return list(self.gw.user_trades)
        raise AssertionError(path)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.position_amt = 0.0
        self.entry_px = 60_000.0
        self.algo_delay_s = 0.0
        self.algo_reject: set[str] = set()
        self.user_trades: list[dict[str, Any]] = []
        self.rest = FakeRest(self)
        self.liq = 0.0

    async def ensure_one_way(self) -> None:
        self.calls.append(("one_way", None))

    async def ensure_symbol(self, symbol: str, leverage: int) -> None:
        self.calls.append(("ensure", (symbol, leverage)))

    async def place_order(
        self, si: SymbolInfo, side: str, type_: str, qty: Decimal, client_id: str, **kw: Any
    ) -> dict[str, Any]:
        self.calls.append(("order", (side, type_, qty, client_id, kw)))
        if type_ == "MARKET":
            sign = 1 if side == "BUY" else -1
            if kw.get("reduce_only") and self.position_amt == 0:
                raise OrderRejectedError(-2022, "ReduceOnly Order is rejected.")
            self.position_amt += sign * float(qty)
            return {"executedQty": str(qty), "avgPrice": str(self.entry_px), "status": "FILLED"}
        return {"status": "NEW"}

    async def get_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        return {"executedQty": "0", "avgPrice": "0", "status": "CANCELED"}

    async def cancel_order(self, symbol: str, client_id: str) -> None:
        self.calls.append(("cancel_order", client_id))

    async def place_algo(
        self, si: SymbolInfo, side: str, type_: str, trigger: Decimal, cid: str, **kw: Any
    ) -> dict[str, Any]:
        if self.algo_delay_s:
            await asyncio.sleep(self.algo_delay_s)
        self.calls.append(("algo", (side, type_, trigger, cid, kw)))
        if type_ in self.algo_reject:
            raise OrderRejectedError(-4130, "exists")
        return {"algoStatus": "NEW", "clientAlgoId": cid}

    async def cancel_algo(self, cid: str) -> bool:
        self.calls.append(("cancel_algo", cid))
        return True

    async def cancel_all(self, symbol: str) -> None:
        self.calls.append(("cancel_all", symbol))

    async def positions(self) -> list[dict[str, Any]]:
        if self.position_amt == 0:
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": str(self.position_amt),
                "entryPrice": str(self.entry_px),
                "liquidationPrice": str(self.liq),
            }
        ]

    async def open_orders(self) -> list[dict[str, Any]]:
        return []

    async def open_algo_orders(self) -> list[dict[str, Any]]:
        return [{"symbol": "BTCUSDT", "orderType": "STOP_MARKET"}] if self.position_amt else []

    async def income(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"income": "-0.5"}]

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeCtx:
    def __init__(self, cfg: AppConfig | None = None) -> None:
        self.cfg = cfg or load_config("config.yaml")
        self.meta = ExchangeMetadata(BinanceRest("https://x"))
        self.meta.symbols = {"BTCUSDT": SI}
        self.meta.brackets = {"BTCUSDT": BR}
        self.journal = Journal(make_engine("sqlite:///:memory:"), "demo")
        self.discipline = Discipline(self.cfg.discipline)
        self.risk_scale = 1.0
        self.notes: list[tuple[str, str]] = []
        self.edge_checked: list[list[str]] = []
        self.equity = 10_000.0
        self.dd = 0.0
        self._now = 1_791_700_000_000
        self.bt = BookTicker("BTCUSDT", 1, 59_999.9, 1, 60_000.0, 1, self._now)
        self.mark_px = 60_000.0
        self.block: list[str] = []

    def now_ms(self) -> int:
        return self._now

    def bankroll(self) -> float:
        return self.equity

    def drawdown(self) -> float:
        return self.dd

    def daily_loss(self) -> float:
        return 0.0

    def blockers(self, symbol: str, regime: str) -> list[str]:
        return list(self.block)

    def cluster(self, symbol: str) -> int:
        return 0

    def real_stats(self, game_id: str) -> tuple[float, float]:
        return 0.2, 2.0

    def book(self, symbol: str) -> BookTicker | None:
        return self.bt

    def mark(self, symbol: str) -> float | None:
        return self.mark_px

    def costs(self, symbol: str) -> Costs:
        return Costs()

    async def notify(self, kind: str, text: str) -> None:
        self.notes.append((kind, text))

    async def edge_check(self, game_ids: list[str]) -> None:
        self.edge_checked.append(game_ids)

    def persist_discipline(self) -> None:
        pass
