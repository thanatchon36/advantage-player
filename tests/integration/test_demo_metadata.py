"""DEMO-only, read-only. Skipped when DEMO keys are absent."""

from __future__ import annotations

import pytest

from advantage_player.config.settings import Secrets
from advantage_player.exchange.endpoints import ENDPOINTS
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.rest import BinanceRest

pytestmark = pytest.mark.integration


async def test_demo_metadata_roundtrip(demo_secrets: Secrets) -> None:
    rest = BinanceRest(
        ENDPOINTS[demo_secrets.binance_env].rest, demo_secrets.api_key, demo_secrets.api_secret
    )
    try:
        await rest.sync_time()
        assert rest.clock.drift_ms < 5_000
        meta = ExchangeMetadata(rest)
        await meta.refresh_all(["BTCUSDT"])
        assert meta.symbols["BTCUSDT"].tick_size > 0
        assert meta.brackets["BTCUSDT"].max_leverage(1_000) >= meta.brackets[
            "BTCUSDT"
        ].max_leverage(1_000_000)
        assert 0 <= meta.fees["BTCUSDT"].taker < 0.01
        assert rest.limiter.headroom() > 0.5
    finally:
        await rest.aclose()
