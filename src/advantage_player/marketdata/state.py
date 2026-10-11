"""Latest market state per symbol + stale detection + rolling spread statistics."""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

from advantage_player.marketdata.events import AggTrade, BookTicker, Depth, MarketEvent, MarkPrice


@dataclass
class SymbolState:
    symbol: str
    book: BookTicker | None = None
    depth: Depth | None = None
    mark: MarkPrice | None = None
    last_trade: AggTrade | None = None
    last_agg_id: int = -1
    last_book_id: int = -1
    last_seen_ms: dict[str, int] = field(default_factory=dict)
    # (ms, spread_bps) samples, at most one per second, 24 h
    spreads: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=86_400))


class MarketState:
    def __init__(self) -> None:
        self.symbols: dict[str, SymbolState] = {}

    def get(self, symbol: str) -> SymbolState:
        st = self.symbols.get(symbol)
        if st is None:
            st = self.symbols[symbol] = SymbolState(symbol)
        return st

    def apply(self, ev: MarketEvent, recv_ms: int) -> bool:
        """Update state. Returns False for duplicates (overlapping reconnects)."""
        st = self.get(ev.symbol)
        if isinstance(ev, AggTrade):
            if ev.agg_id <= st.last_agg_id:
                return False
            st.last_agg_id = ev.agg_id
            st.last_trade = ev
            st.last_seen_ms["trade"] = recv_ms
        elif isinstance(ev, BookTicker):
            if ev.update_id <= st.last_book_id:
                return False
            st.last_book_id = ev.update_id
            st.book = ev
            st.last_seen_ms["book"] = recv_ms
            if not st.spreads or recv_ms - st.spreads[-1][0] >= 1000:
                st.spreads.append((recv_ms, ev.spread_bps))
        elif isinstance(ev, Depth):
            st.depth = ev
            st.last_seen_ms["depth"] = recv_ms
        elif isinstance(ev, MarkPrice):
            st.mark = ev
            st.last_seen_ms["mark"] = recv_ms
        return True

    def stale_reasons(
        self, symbol: str, now_ms: int, max_age_s: float, kinds: tuple[str, ...]
    ) -> list[str]:
        """Kinds with no update within max_age_s (a never-seen kind counts as stale).

        Trades can legitimately pause, so callers typically check ("book", "mark").
        """
        st = self.symbols.get(symbol)
        seen = st.last_seen_ms if st else {}
        lim = max_age_s * 1000
        return [k for k in kinds if now_ms - seen.get(k, -(10**15)) > lim]

    def median_spread_bps(self, symbol: str, since_ms: int) -> float | None:
        st = self.symbols.get(symbol)
        if not st:
            return None
        vals = [s for t, s in st.spreads if t >= since_ms]
        return statistics.median(vals) if vals else None
