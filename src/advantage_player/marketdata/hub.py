"""Wires streams → parse → MarketState → recorder → bar builders → bar subscribers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Any

import structlog

from advantage_player.marketdata.bars import Bar, BarBuilder
from advantage_player.marketdata.events import AggTrade, MarketEvent, parse_event
from advantage_player.marketdata.recorder import TickRecorder
from advantage_player.marketdata.state import MarketState
from advantage_player.marketdata.streams import StreamManager

log = structlog.get_logger(__name__)
BarCallback = Callable[[Bar], None]
EventCallback = Callable[[MarketEvent], None]


class MarketDataHub:
    def __init__(
        self,
        ws_base: str,
        timeframes: Iterable[str],
        recorder: TickRecorder | None = None,
        max_streams_per_conn: int = 200,
    ) -> None:
        self.state = MarketState()
        self.recorder = recorder
        self.timeframes = sorted(set(timeframes))
        self.builders: dict[tuple[str, str], BarBuilder] = {}
        self.bar_subscribers: list[BarCallback] = []
        self.event_subscribers: list[EventCallback] = []
        self.streams = StreamManager(ws_base, self.on_message, max_streams_per_conn)
        self.counts: dict[str, int] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def set_symbols(self, full: Iterable[str], light: Iterable[str]) -> None:
        full, light = list(full), list(light)
        for sym in [*full, *light]:
            for tf in self.timeframes:
                self.builders.setdefault((sym, tf), BarBuilder(sym, tf))
        await self.streams.set_symbols(full, light)

    def on_message(self, stream: str, data: dict[str, Any]) -> None:
        ev = parse_event(data)
        if ev is None:
            return
        self.handle(ev, int(time.time() * 1000))

    def handle(self, ev: MarketEvent, recv_ms: int) -> None:
        if not self.state.apply(ev, recv_ms):
            return
        kind = type(ev).__name__
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self.recorder:
            self.recorder.record(ev)
        for cb in self.event_subscribers:
            cb(ev)
        if isinstance(ev, AggTrade):
            for tf in self.timeframes:
                b = self.builders.get((ev.symbol, tf))
                if b:
                    self._emit(b.add(ev))

    def _emit(self, bars: list[Bar]) -> None:
        for bar in bars:
            for cb in self.bar_subscribers:
                cb(bar)

    def flush_bars(self, now_ms: int) -> None:
        for b in self.builders.values():
            self._emit(b.flush(now_ms))

    async def start(self) -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(1.0)
                self.flush_bars(int(time.time() * 1000) - 250)  # small grace for late trades

        self._flush_task = asyncio.create_task(loop(), name="bar-flush")

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        await self.streams.stop()
        if self.recorder:
            self.recorder.flush()
