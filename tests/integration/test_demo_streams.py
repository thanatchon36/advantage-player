"""DEMO-only, read-only market data. Skipped when DEMO keys are absent."""

from __future__ import annotations

import asyncio
import time

import pytest

from advantage_player.config.settings import Secrets
from advantage_player.exchange.endpoints import ENDPOINTS
from advantage_player.marketdata.hub import MarketDataHub

pytestmark = pytest.mark.integration


async def test_demo_routed_streams_deliver(demo_secrets: Secrets) -> None:
    hub = MarketDataHub(ENDPOINTS[demo_secrets.binance_env].ws_stream, ["5s"])
    await hub.set_symbols(["BTCUSDT"], [])
    await hub.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(hub.counts) < 4:
            await asyncio.sleep(0.5)
    finally:
        await hub.stop()
    assert set(hub.counts) == {"AggTrade", "BookTicker", "Depth", "MarkPrice"}
    now = int(time.time() * 1000)
    assert hub.state.stale_reasons("BTCUSDT", now, 30, ("book", "mark", "depth")) == []
