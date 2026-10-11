"""Local OHLCV bars from aggTrade (sub-minute TFs aren't offered by Binance klines).

Buckets are aligned to exchange trade time T. Gaps produce flat zero-volume bars carrying
the previous close, so indicators see a regular time grid.
"""

from __future__ import annotations

from dataclasses import dataclass

from advantage_player.marketdata.events import AggTrade

_UNITS = {"s": 1, "m": 60, "h": 3600}


def tf_seconds(tf: str) -> int:
    return int(tf[:-1]) * _UNITS[tf[-1]]


@dataclass(slots=True)
class Bar:
    symbol: str
    tf_s: int
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    quote_volume: float = 0.0
    buy_volume: float = 0.0  # aggressive buys
    n_trades: int = 0

    @property
    def close_ms(self) -> int:
        return self.open_ms + self.tf_s * 1000

    @property
    def sell_volume(self) -> float:
        return self.volume - self.buy_volume

    @property
    def delta(self) -> float:
        """Aggressive buy volume minus aggressive sell volume."""
        return 2 * self.buy_volume - self.volume

    @property
    def vwap(self) -> float:
        return self.quote_volume / self.volume if self.volume else self.close


class BarBuilder:
    def __init__(self, symbol: str, tf: str) -> None:
        self.symbol = symbol
        self.tf_s = tf_seconds(tf)
        self.cur: Bar | None = None

    def _bucket(self, ms: int) -> int:
        step = self.tf_s * 1000
        return ms - ms % step

    def _roll_to(self, bucket: int) -> list[Bar]:
        """Close bars up to (not incl.) `bucket`, filling gaps with flat bars."""
        closed: list[Bar] = []
        assert self.cur is not None
        while self.cur.open_ms < bucket:
            closed.append(self.cur)
            px = self.cur.close
            self.cur = Bar(self.symbol, self.tf_s, self.cur.close_ms, px, px, px, px)
        return closed

    def add(self, t: AggTrade) -> list[Bar]:
        b = self._bucket(t.trade_ms)
        closed: list[Bar] = []
        if self.cur is None:
            self.cur = Bar(self.symbol, self.tf_s, b, t.price, t.price, t.price, t.price)
        elif b > self.cur.open_ms:
            closed = self._roll_to(b)
        bar = self.cur
        if bar.n_trades == 0:
            bar.open = bar.high = bar.low = t.price
        bar.high = max(bar.high, t.price)
        bar.low = min(bar.low, t.price)
        bar.close = t.price
        bar.volume += t.qty
        bar.quote_volume += t.qty * t.price
        if not t.buyer_maker:
            bar.buy_volume += t.qty
        bar.n_trades += 1
        return closed

    def flush(self, now_ms: int) -> list[Bar]:
        """Close bars whose window has fully elapsed (call periodically from a timer)."""
        if self.cur is None:
            return []
        return self._roll_to(self._bucket(now_ms))
