"""Strategy plug-in contract: `on_bar(ctx) -> Signal | None` (SPEC §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from advantage_player.config.settings import GameCfg
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.events import BookTicker, Depth
from advantage_player.strategies.indicators import Regime, Snapshot

Side = Literal["BUY", "SELL"]
EntryType = Literal["market", "maker"]


def game_id(strategy: str, symbol: str, tf: str, regime: Regime | str) -> str:
    """A game = (strategy, symbol, timeframe, regime)."""
    return f"{strategy}|{symbol}|{tf}|{regime}"


@dataclass(frozen=True)
class Target:
    r: float  # R-multiple from entry
    fraction: float  # of the original size
    maker: bool = False  # post-only reduce-only limit (else TAKE_PROFIT_MARKET algo)


@dataclass(frozen=True)
class Signal:
    side: Side
    entry_type: EntryType
    entry_ref: float  # reference price at signal time (limit price for maker entries)
    stop: float
    targets: tuple[Target, ...]
    tag: str
    move_stop_to_be_after_first_target: bool = False
    trail_atr: float = 0.0  # >0: trail the remainder by this many ATR after first target
    atr: float = 0.0
    time_stop_bars: int = 0

    @property
    def direction(self) -> int:
        return 1 if self.side == "BUY" else -1

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_ref - self.stop)

    def target_price(self, t: Target, entry: float | None = None) -> float:
        e = self.entry_ref if entry is None else entry
        return e + self.direction * t.r * self.risk_per_unit


@dataclass
class StrategyContext:
    symbol: str
    tf: str
    bars: list[Bar]  # most recent last; bars[-1] just closed
    snap: Snapshot
    book: BookTicker | None = None
    depth: Depth | None = None
    tick_size: float = 0.0
    imbalance_since_ms: dict[int, int] = field(default_factory=dict)  # side → ms since persistent
    now_ms: int = 0

    @property
    def bar(self) -> Bar:
        return self.bars[-1]


class Strategy(Protocol):
    name: str
    cfg: GameCfg

    def on_bar(self, ctx: StrategyContext) -> Signal | None: ...
