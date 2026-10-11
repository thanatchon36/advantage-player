"""Raw tick recorder → Parquet, partitioned data/ticks/<kind>/<YYYY-MM-DD>/<SYMBOL>/part-*.parquet.

Buffers in memory and flushes on size or interval; each flush writes a new part file so a
crash loses at most one buffer. Feeds the RESEARCH replay engine.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from advantage_player.marketdata.events import AggTrade, BookTicker, Depth, MarketEvent, MarkPrice

KIND = {AggTrade: "aggTrade", BookTicker: "bookTicker", Depth: "depth", MarkPrice: "markPrice"}
DEPTH_LEVELS = 5


def _row(ev: MarketEvent) -> dict[str, Any]:
    if isinstance(ev, Depth):
        row: dict[str, Any] = {"symbol": ev.symbol, "event_ms": ev.event_ms}
        for side, levels in (("b", ev.bids), ("a", ev.asks)):
            for i in range(DEPTH_LEVELS):
                px, q = levels[i] if i < len(levels) else (float("nan"), 0.0)
                row[f"{side}{i}_px"], row[f"{side}{i}_q"] = px, q
        return row
    return asdict(ev)


def _ts(ev: MarketEvent) -> int:
    return ev.trade_ms if isinstance(ev, AggTrade) else ev.event_ms


class TickRecorder:
    def __init__(self, root: str | Path, flush_rows: int = 20_000, flush_s: float = 60.0) -> None:
        self.root = Path(root) / "ticks"
        self.flush_rows = flush_rows
        self.flush_s = flush_s
        self._buf: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self._rows = 0
        self._last_flush = time.monotonic()
        self.files_written = 0

    def record(self, ev: MarketEvent) -> None:
        day = datetime.fromtimestamp(_ts(ev) / 1000, UTC).strftime("%Y-%m-%d")
        self._buf[(KIND[type(ev)], day, ev.symbol)].append(_row(ev))
        self._rows += 1
        if self._rows >= self.flush_rows or time.monotonic() - self._last_flush >= self.flush_s:
            self.flush()

    def flush(self) -> None:
        stamp = time.time_ns()
        for (kind, day, sym), rows in self._buf.items():
            if not rows:
                continue
            d = self.root / kind / day / sym
            d.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(rows), d / f"part-{stamp}.parquet")
            self.files_written += 1
        self._buf.clear()
        self._rows = 0
        self._last_flush = time.monotonic()


def load_ticks(root: str | Path, kind: str, symbol: str, day: str) -> pa.Table:
    d = Path(root) / "ticks" / kind / day / symbol
    files = sorted(d.glob("part-*.parquet"))
    if not files:
        return pa.table({})
    return pa.concat_tables([pq.read_table(f) for f in files])
