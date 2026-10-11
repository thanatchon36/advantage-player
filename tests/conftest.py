from __future__ import annotations

import os

import pytest

from advantage_player.config.settings import Secrets


@pytest.fixture
def demo_secrets() -> Secrets:
    s = Secrets()
    if not s.has_keys or os.environ.get("AP_SKIP_INTEGRATION") == "1":
        pytest.skip("DEMO API keys not configured")
    if s.binance_env.value != "demo":
        pytest.skip("integration tests run on DEMO only")
    return s
