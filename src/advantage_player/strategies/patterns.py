"""Candlestick triggers. All take bars oldest→newest; direction +1 bullish, −1 bearish."""

from __future__ import annotations

from advantage_player.marketdata.bars import Bar


def body(b: Bar) -> float:
    return abs(b.close - b.open)


def upper_wick(b: Bar) -> float:
    return b.high - max(b.open, b.close)


def lower_wick(b: Bar) -> float:
    return min(b.open, b.close) - b.low


def engulfing(prev: Bar, cur: Bar, direction: int) -> bool:
    if direction > 0:
        return (
            prev.close < prev.open
            and cur.close > cur.open
            and cur.close >= prev.open
            and cur.open <= prev.close
            and body(cur) > body(prev)
        )
    return (
        prev.close > prev.open
        and cur.close < cur.open
        and cur.close <= prev.open
        and cur.open >= prev.close
        and body(cur) > body(prev)
    )


def pin_bar(b: Bar, direction: int, ratio: float = 2.0) -> bool:
    """Hammer (direction +1) / shooting star (−1): long rejecting wick, small opposite wick."""
    rng = b.high - b.low
    if rng <= 0:
        return False
    bd = max(body(b), rng * 0.05)
    if direction > 0:
        return lower_wick(b) >= ratio * bd and upper_wick(b) <= bd
    return upper_wick(b) >= ratio * bd and lower_wick(b) <= bd


def inside_bar_breakout(mother: Bar, inside: Bar, cur: Bar, direction: int) -> bool:
    if not (inside.high <= mother.high and inside.low >= mother.low):
        return False
    return cur.close > inside.high if direction > 0 else cur.close < inside.low


def trigger(bars: list[Bar], direction: int) -> tuple[str, float] | None:
    """Return (pattern name, pattern extreme) if any trigger fires on the last bar."""
    if len(bars) < 3:
        return None
    m, p, c = bars[-3], bars[-2], bars[-1]

    def ext(*bs: Bar) -> float:
        return min(b.low for b in bs) if direction > 0 else max(b.high for b in bs)

    if engulfing(p, c, direction):
        return "engulfing", ext(p, c)
    if pin_bar(c, direction):
        return ("hammer" if direction > 0 else "shooting_star"), ext(c)
    if inside_bar_breakout(m, p, c, direction):
        return "inside_breakout", ext(p, c)
    return None
