"""Per-symbol exchange metadata: filters, leverage brackets, fees."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Literal

Rounding = Literal["down", "up", "nearest"]
_MODES = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}


def round_to_step(value: Decimal | float | str, step: Decimal, mode: Rounding = "down") -> Decimal:
    v = Decimal(str(value))
    n = (v / step).to_integral_value(rounding=_MODES[mode])
    return (n * step).quantize(step)


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    onboard_ms: int
    delivery_ms: int
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_max_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, s: dict[str, Any]) -> SymbolInfo:
        f = {x["filterType"]: x for x in s["filters"]}
        lot = f["LOT_SIZE"]
        mkt = f.get("MARKET_LOT_SIZE", lot)
        return cls(
            symbol=s["symbol"],
            status=s["status"],
            contract_type=s["contractType"],
            quote_asset=s["quoteAsset"],
            onboard_ms=int(s["onboardDate"]),
            delivery_ms=int(s["deliveryDate"]),
            tick_size=Decimal(f["PRICE_FILTER"]["tickSize"]),
            step_size=Decimal(lot["stepSize"]),
            min_qty=Decimal(lot["minQty"]),
            max_qty=Decimal(lot["maxQty"]),
            market_step_size=Decimal(mkt["stepSize"]),
            market_max_qty=Decimal(mkt["maxQty"]),
            min_notional=Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "0")),
        )

    def round_price(self, price: Decimal | float | str, mode: Rounding = "nearest") -> Decimal:
        return round_to_step(price, self.tick_size, mode)

    def round_qty(self, qty: Decimal | float | str, market: bool = False) -> Decimal:
        """Always floors: never size up past what risk allowed."""
        step = self.market_step_size if market else self.step_size
        cap = self.market_max_qty if market else self.max_qty
        return min(round_to_step(qty, step, "down"), cap)

    def tick_bps(self, price: float) -> float:
        return float(self.tick_size) / price * 1e4

    def meets_min_notional(self, qty: Decimal, price: Decimal) -> bool:
        return qty >= self.min_qty and qty * price >= self.min_notional


@dataclass(frozen=True)
class Bracket:
    bracket: int
    initial_leverage: int
    notional_floor: float
    notional_cap: float
    maint_margin_ratio: float
    cum: float


@dataclass(frozen=True)
class LeverageBrackets:
    symbol: str
    brackets: tuple[Bracket, ...]  # sorted by notional_floor

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> LeverageBrackets:
        bs = sorted(
            (
                Bracket(
                    bracket=int(b["bracket"]),
                    initial_leverage=int(b["initialLeverage"]),
                    notional_floor=float(b["notionalFloor"]),
                    notional_cap=float(b["notionalCap"]),
                    maint_margin_ratio=float(b["maintMarginRatio"]),
                    cum=float(b["cum"]),
                )
                for b in item["brackets"]
            ),
            key=lambda b: b.notional_floor,
        )
        return cls(symbol=item["symbol"], brackets=tuple(bs))

    def for_notional(self, notional: float) -> Bracket:
        for b in self.brackets:
            if b.notional_floor <= notional < b.notional_cap:
                return b
        if notional >= self.brackets[-1].notional_cap:
            # beyond the last cap the exchange won't accept the position at any leverage
            raise ValueError(f"{self.symbol}: notional {notional} exceeds max bracket cap")
        return self.brackets[0]

    def max_leverage(self, notional: float) -> int:
        return self.for_notional(notional).initial_leverage

    def mmr(self, notional: float) -> float:
        return self.for_notional(notional).maint_margin_ratio


@dataclass(frozen=True)
class Fees:
    symbol: str
    maker: float  # fraction, e.g. 0.0002
    taker: float

    @property
    def maker_bps(self) -> float:
        return self.maker * 1e4

    @property
    def taker_bps(self) -> float:
        return self.taker * 1e4
