"""Server-time synchronisation.

offset_ms = server_time - local_midpoint, taken from the lowest-RTT of N samples.
Signed requests use local_now + offset so they're valid regardless of local drift;
`drift_ms` (|offset|) feeds the clock-drift entry blocker (SPEC §7.10).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class TimeSample:
    sent_ms: float
    recv_ms: float
    server_ms: float

    @property
    def rtt_ms(self) -> float:
        return self.recv_ms - self.sent_ms

    @property
    def offset_ms(self) -> float:
        return self.server_ms - (self.sent_ms + self.recv_ms) / 2


def best_offset(samples: list[TimeSample]) -> TimeSample:
    if not samples:
        raise ValueError("no time samples")
    return min(samples, key=lambda s: s.rtt_ms)


class Clock:
    def __init__(self, local_ms: Callable[[], float] | None = None) -> None:
        self._local_ms = local_ms or (lambda: time.time() * 1000)
        self.offset_ms: float = 0.0
        self.rtt_ms: float = 0.0
        self.synced_at_ms: float | None = None

    def local_ms(self) -> float:
        return self._local_ms()

    def now_ms(self) -> int:
        return int(self._local_ms() + self.offset_ms)

    @property
    def drift_ms(self) -> float:
        return abs(self.offset_ms)

    async def sync(self, fetch_server_ms: Callable[[], Awaitable[float]], n: int = 5) -> None:
        samples = []
        for _ in range(n):
            t0 = self._local_ms()
            server = await fetch_server_ms()
            t1 = self._local_ms()
            samples.append(TimeSample(t0, t1, server))
        best = best_offset(samples)
        self.offset_ms = best.offset_ms
        self.rtt_ms = best.rtt_ms
        self.synced_at_ms = self._local_ms()
