"""Typed market events parsed from Binance stream payloads (field names verified on DEMO)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AggTrade:
    symbol: str
    agg_id: int
    price: float
    qty: float
    trade_ms: int
    buyer_maker: bool  # True → aggressor was the SELLER

    @property
    def signed_qty(self) -> float:
        """+qty for aggressive buys, −qty for aggressive sells."""
        return -self.qty if self.buyer_maker else self.qty


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    update_id: int
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    event_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4


@dataclass(frozen=True, slots=True)
class Depth:
    symbol: str
    event_ms: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    def imbalance(self, levels: int = 5) -> float:
        """Bid share of top-N quantity, in [0, 1]. 0.5 = balanced."""
        b = sum(q for _, q in self.bids[:levels])
        a = sum(q for _, q in self.asks[:levels])
        return b / (a + b) if a + b > 0 else 0.5


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    event_ms: int
    mark: float
    index: float
    funding_rate: float
    next_funding_ms: int


MarketEvent = AggTrade | BookTicker | Depth | MarkPrice


def parse_event(data: dict[str, Any]) -> MarketEvent | None:
    e = data.get("e")
    if e == "aggTrade":
        return AggTrade(
            data["s"],
            int(data["a"]),
            float(data["p"]),
            float(data["q"]),
            int(data["T"]),
            bool(data["m"]),
        )
    if e == "bookTicker":
        return BookTicker(
            data["s"],
            int(data["u"]),
            float(data["b"]),
            float(data["B"]),
            float(data["a"]),
            float(data["A"]),
            int(data.get("E", data.get("T", 0))),
        )
    if e == "depthUpdate":
        return Depth(
            data["s"],
            int(data["E"]),
            tuple((float(p), float(q)) for p, q in data["b"]),
            tuple((float(p), float(q)) for p, q in data["a"]),
        )
    if e == "markPriceUpdate":
        return MarkPrice(
            data["s"],
            int(data["E"]),
            float(data["p"]),
            float(data["i"]),
            float(data.get("r") or 0.0),
            int(data.get("T") or 0),
        )
    return None
