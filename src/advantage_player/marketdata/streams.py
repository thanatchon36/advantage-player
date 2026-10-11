"""Sharded, routed combined-stream connections.

Routing (Binance USDⓈ-M): high-frequency book data on /public, regular market data on
/market. A stream on the wrong route is silently dropped on LIVE (DEMO delivers either
way), so per-symbol stale detection in MarketState is the safety net.

Subscriptions go in the URL (no outbound SUBSCRIBE messages → the 10 msg/s inbound limit
is never at risk). Connections rotate proactively before Binance's 24 h forced disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
import websockets

log = structlog.get_logger(__name__)

PUBLIC_PREFIXES = ("depth", "bookTicker")
Handler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


def route_for(stream: str) -> str:
    kind = stream.split("@", 1)[1] if "@" in stream else stream
    return "public" if kind.startswith(PUBLIC_PREFIXES) else "market"


def symbol_streams(symbol: str, full: bool) -> list[str]:
    s = symbol.lower()
    base = [f"{s}@aggTrade", f"{s}@markPrice@1s"]
    return [*base, f"{s}@bookTicker", f"{s}@depth5@100ms"] if full else base


def shard(streams: Iterable[str], max_per_conn: int) -> list[tuple[str, list[str]]]:
    """Group by route, then chunk. Returns [(route, streams)]."""
    by_route: dict[str, list[str]] = {}
    for st in sorted(set(streams)):
        by_route.setdefault(route_for(st), []).append(st)
    out: list[tuple[str, list[str]]] = []
    for route in sorted(by_route):
        lst = by_route[route]
        out += [(route, lst[i : i + max_per_conn]) for i in range(0, len(lst), max_per_conn)]
    return out


@dataclass
class ShardConn:
    base_url: str
    route: str
    streams: list[str]
    handler: Handler
    rotate_after_s: float = 23 * 3600
    connected: bool = False
    reconnects: int = 0
    last_msg_s: float = 0.0
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.route}/stream?streams={'/'.join(self.streams)}"

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"ws-{self.route}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.url, open_timeout=10, ping_interval=60, ping_timeout=60, max_size=2**22
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    opened = time.monotonic()
                    log.info("ws_connected", route=self.route, streams=len(self.streams))
                    while time.monotonic() - opened < self.rotate_after_s:
                        remaining = self.rotate_after_s - (time.monotonic() - opened)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
                        except TimeoutError:
                            continue
                        self.last_msg_s = time.monotonic()
                        msg = json.loads(raw)
                        if "stream" in msg:
                            r = self.handler(msg["stream"], msg["data"])
                            if r is not None:
                                await r
                    log.info("ws_rotate", route=self.route)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                self.connected = False
                self.reconnects += 1
                log.warning("ws_error", route=self.route, error=repr(e), backoff_s=backoff)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)
            self.connected = False


class StreamManager:
    def __init__(self, base_url: str, handler: Handler, max_streams_per_conn: int = 200) -> None:
        self.base_url = base_url
        self.handler = handler
        self.max_per_conn = max_streams_per_conn
        self.conns: list[ShardConn] = []
        self.streams: set[str] = set()

    async def set_symbols(self, full: Iterable[str], light: Iterable[str]) -> None:
        wanted = {s for sym in full for s in symbol_streams(sym, True)}
        wanted |= {s for sym in light for s in symbol_streams(sym, False)}
        if wanted == self.streams:
            return
        await self.stop()
        self.streams = wanted
        for route, chunk in shard(wanted, self.max_per_conn):
            c = ShardConn(self.base_url, route, chunk, self.handler)
            c.start()
            self.conns.append(c)

    async def stop(self) -> None:
        for c in self.conns:
            await c.stop()
        self.conns = []

    @property
    def healthy(self) -> bool:
        return bool(self.conns) and all(c.connected for c in self.conns)
