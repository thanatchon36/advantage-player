"""Cache of exchangeInfo filters, leverage brackets and commission rates.

Refreshed at startup and daily (SPEC §1). Never hardcode leverage or fees.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from advantage_player.exchange.models import Fees, LeverageBrackets, SymbolInfo
from advantage_player.exchange.rest import BinanceAPIError, BinanceRest

log = structlog.get_logger(__name__)
DAY_MS = 86_400_000


class ExchangeMetadata:
    def __init__(self, rest: BinanceRest) -> None:
        self.rest = rest
        self.symbols: dict[str, SymbolInfo] = {}
        self.brackets: dict[str, LeverageBrackets] = {}
        self.fees: dict[str, Fees] = {}
        self.refreshed_ms: float = 0.0

    async def refresh_exchange_info(self) -> None:
        data: dict[str, Any] = await self.rest.public("GET", "/fapi/v1/exchangeInfo")
        self.rest.limiter.set_limits(data.get("rateLimits", []))
        self.symbols = {s["symbol"]: SymbolInfo.from_exchange_info(s) for s in data["symbols"]}
        log.info("exchange_info_refreshed", symbols=len(self.symbols))

    async def refresh_brackets(self) -> None:
        data = await self.rest.signed("GET", "/fapi/v1/leverageBracket")
        items = data if isinstance(data, list) else [data]
        self.brackets = {i["symbol"]: LeverageBrackets.from_api(i) for i in items}
        log.info("brackets_refreshed", symbols=len(self.brackets))

    async def refresh_fees(self, symbols: list[str]) -> None:
        for sym in symbols:
            try:
                d = await self.rest.signed("GET", "/fapi/v1/commissionRate", {"symbol": sym})
            except BinanceAPIError as e:
                # DEMO returns HTTP 500 for some symbols; callers fall back to conservative fees
                log.warning("commission_rate_failed", symbol=sym, error=str(e))
                continue
            self.fees[sym] = Fees(
                symbol=sym,
                maker=float(d["makerCommissionRate"]),
                taker=float(d["takerCommissionRate"]),
            )

    async def refresh_all(self, fee_symbols: list[str]) -> None:
        await self.refresh_exchange_info()
        await self.refresh_brackets()
        await self.refresh_fees(fee_symbols)
        self.refreshed_ms = time.time() * 1000

    def stale(self, now_ms: float | None = None) -> bool:
        now = now_ms if now_ms is not None else time.time() * 1000
        return now - self.refreshed_ms > DAY_MS

    def perpetuals(self, quote: str = "USDT") -> list[SymbolInfo]:
        return [
            s
            for s in self.symbols.values()
            if s.contract_type == "PERPETUAL" and s.quote_asset == quote
        ]
