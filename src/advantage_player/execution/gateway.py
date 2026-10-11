"""Typed order / position calls with idempotent client ids (codes verified on DEMO 2026-10-11).

-4120 conditional type on /fapi/v1/order → must use /fapi/v1/algoOrder
-5022 post-only (GTX) would cross → rejected, not recorded
-4046 margin type already set (benign)
-4130 a closePosition STOP/TP in that direction already exists (one per direction!)
-2022 reduce-only rejected (e.g. no position)
-2011 / -2013 unknown order (cancel / query of a non-existent order)
Unknown status (timeout / 503 unknown) → query by client id before deciding.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import structlog

from advantage_player.exchange.models import SymbolInfo
from advantage_player.exchange.rest import BinanceAPIError, BinanceRest, UnknownStatusError

log = structlog.get_logger(__name__)
BENIGN_CANCEL = {-2011, -2013}


class OrderRejectedError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


def fmt(x: Decimal | float) -> str:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    s = format(d.normalize(), "f")
    return s


class Gateway:
    def __init__(self, rest: BinanceRest) -> None:
        self.rest = rest
        self._leverage: dict[str, int] = {}
        self._isolated: set[str] = set()

    # ---- account setup ---------------------------------------------------------------------
    async def ensure_one_way(self) -> None:
        r = await self.rest.signed("GET", "/fapi/v1/positionSide/dual")
        if r.get("dualSidePosition"):
            await self.rest.signed(
                "POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}
            )

    async def ensure_symbol(self, symbol: str, leverage: int) -> None:
        if symbol not in self._isolated:
            try:
                await self.rest.signed(
                    "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}
                )
            except BinanceAPIError as e:
                if e.code != -4046:
                    raise
            self._isolated.add(symbol)
        if self._leverage.get(symbol) != leverage:
            await self.rest.signed(
                "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}
            )
            self._leverage[symbol] = leverage

    # ---- orders ----------------------------------------------------------------------------
    async def place_order(
        self,
        si: SymbolInfo,
        side: str,
        type_: str,
        qty: Decimal,
        client_id: str,
        *,
        price: Decimal | None = None,
        tif: str | None = None,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "symbol": si.symbol,
            "side": side,
            "type": type_,
            "quantity": fmt(qty),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if price is not None:
            p["price"] = fmt(price)
        if tif:
            p["timeInForce"] = tif
        if reduce_only:
            p["reduceOnly"] = "true"
        try:
            return dict(await self.rest.signed("POST", "/fapi/v1/order", p))
        except UnknownStatusError:
            found = await self._find_order(si.symbol, client_id)
            if found is None:
                raise OrderRejectedError(
                    0, "unknown status; order not found after retries"
                ) from None
            return found
        except BinanceAPIError as e:
            raise OrderRejectedError(e.code, e.msg) from e

    async def _find_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        for _ in range(3):
            await asyncio.sleep(1.0)
            try:
                return dict(
                    await self.rest.signed(
                        "GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}
                    )
                )
            except BinanceAPIError as e:
                if e.code not in BENIGN_CANCEL:
                    raise
        return None

    async def cancel_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        try:
            return dict(
                await self.rest.signed(
                    "DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}
                )
            )
        except BinanceAPIError as e:
            if e.code in BENIGN_CANCEL:
                return None
            raise

    async def get_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        try:
            return dict(
                await self.rest.signed(
                    "GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}
                )
            )
        except BinanceAPIError as e:
            if e.code in BENIGN_CANCEL:
                return None
            raise

    # ---- algo (conditional) orders ---------------------------------------------------------
    async def place_algo(
        self,
        si: SymbolInfo,
        side: str,
        type_: str,
        trigger: Decimal,
        client_algo_id: str,
        *,
        close_position: bool = False,
        qty: Decimal | None = None,
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": si.symbol,
            "side": side,
            "type": type_,
            "triggerPrice": fmt(trigger),
            "workingType": "MARK_PRICE",
            "clientAlgoId": client_algo_id,
            "newOrderRespType": "RESULT",
        }
        if close_position:
            p["closePosition"] = "true"
        else:
            assert qty is not None
            p["quantity"] = fmt(qty)
            p["reduceOnly"] = "true"
        try:
            return dict(await self.rest.signed("POST", "/fapi/v1/algoOrder", p))
        except UnknownStatusError:
            for _ in range(3):
                await asyncio.sleep(0.5)
                got = await self.get_algo(client_algo_id)
                if got is not None:
                    return got
            raise OrderRejectedError(0, "algo unknown status; not found") from None
        except BinanceAPIError as e:
            raise OrderRejectedError(e.code, e.msg) from e

    async def get_algo(self, client_algo_id: str) -> dict[str, Any] | None:
        try:
            return dict(
                await self.rest.signed(
                    "GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id}
                )
            )
        except BinanceAPIError as e:
            if e.code in BENIGN_CANCEL or "not exist" in e.msg.lower():
                return None
            raise

    async def cancel_algo(self, client_algo_id: str) -> bool:
        try:
            await self.rest.signed("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id})
            return True
        except BinanceAPIError as e:
            log.info("cancel_algo_failed", id=client_algo_id, code=e.code, msg=e.msg)
            return False

    async def cancel_all(self, symbol: str) -> None:
        """Cancel every open order AND algo order on a symbol (kill / flatten)."""
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                await self.rest.signed("DELETE", path, {"symbol": symbol})
            except BinanceAPIError as e:
                log.warning("cancel_all_failed", symbol=symbol, path=path, code=e.code)

    # ---- state -----------------------------------------------------------------------------
    async def positions(self) -> list[dict[str, Any]]:
        rows = await self.rest.signed("GET", "/fapi/v3/positionRisk")
        return [r for r in rows if float(r["positionAmt"]) != 0]

    async def open_orders(self) -> list[dict[str, Any]]:
        return list(await self.rest.signed("GET", "/fapi/v1/openOrders"))

    async def open_algo_orders(self) -> list[dict[str, Any]]:
        return list(await self.rest.signed("GET", "/fapi/v1/openAlgoOrders"))

    async def account(self) -> dict[str, Any]:
        return dict(await self.rest.signed("GET", "/fapi/v3/account"))

    async def income(
        self, income_type: str, start_ms: int, end_ms: int, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        p: dict[str, Any] = {
            "incomeType": income_type,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        if symbol:
            p["symbol"] = symbol
        return list(await self.rest.signed("GET", "/fapi/v1/income", p))
