from __future__ import annotations

import itertools

import pytest

from advantage_player.exchange.clock import Clock, TimeSample, best_offset
from advantage_player.exchange.ratelimit import RateLimiter


def test_offset_uses_midpoint_of_lowest_rtt_sample() -> None:
    samples = [TimeSample(0, 400, 1500), TimeSample(1000, 1020, 2210)]
    best = best_offset(samples)
    assert best.rtt_ms == 20
    assert best.offset_ms == pytest.approx(2210 - 1010)


async def test_clock_sync_and_now() -> None:
    ticks = itertools.count(0, 10)
    clock = Clock(local_ms=lambda: float(next(ticks)))

    async def server() -> float:
        return 10_000.0

    await clock.sync(server, n=3)
    assert clock.drift_ms > 9000
    assert clock.now_ms() > 10_000


def test_ratelimit_headers_and_limits() -> None:
    rl = RateLimiter()
    rl.set_limits(
        [
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": 6000,
            },
            {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 300},
        ]
    )
    rl.update({"x-mbx-used-weight-1m": "600", "X-MBX-ORDER-COUNT-10S": "270", "other": "1"})
    assert rl.used[("REQUEST_WEIGHT", "1MINUTE")] == 600
    assert rl.used[("ORDERS", "10SECOND")] == 270
    assert rl.headroom() == pytest.approx(0.1)


def test_headroom_defaults_to_full() -> None:
    assert RateLimiter().headroom() == 1.0
