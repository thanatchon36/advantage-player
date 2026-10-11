"""Storage on Postgres (docker-compose `db`). Skipped unless AP_TEST_PG_URL is set, e.g.
AP_TEST_PG_URL=postgresql+psycopg://ap:ap@127.0.0.1:5432/ap uv run pytest tests/integration"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from advantage_player.app import Bot
from advantage_player.config.settings import Secrets, StorageCfg, load_config
from advantage_player.dashboard import backend
from advantage_player.ledger.journal import Journal
from advantage_player.risk import control_queue
from advantage_player.sim.fills import Leg, TradeResult
from advantage_player.storage.db import make_engine
from advantage_player.storage.kv import kv_get

PG = os.environ.get("AP_TEST_PG_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG, reason="AP_TEST_PG_URL not set"),
]


async def test_bot_journal_dashboard_on_postgres(tmp_path: Path) -> None:
    cfg = load_config("config.yaml").model_copy(
        update={"storage": StorageCfg(url=PG, data_dir=str(tmp_path))}
    )
    with make_engine(PG).begin() as c:  # clean slate before the bot loads state
        for t in ("trades", "signals", "kv", "control_requests", "games", "game_transitions"):
            c.execute(text(f"DELETE FROM {t} WHERE env = 'demo'"))
    bot = Bot(Secrets(_env_file=None), cfg)
    ms = int(datetime.now(UTC).timestamp() * 1000)
    tr = TradeResult(
        "A|BTCUSDT|1m|TREND", "BTCUSDT", "BUY", "t" * 100, ms - 120_000, ms - 120_000,
        ms - 1, 100, 100, 99, [Leg(101, 101, 1, 0, "target")], 1, 1.1, 0.1, 0, 0, -0.3,
        1.2, "target", False, None,
    )  # fmt: skip
    j = Journal(bot.db, bot.env)
    j.trade(tr, "PROBE", "TREND", qty=1, pnl_usd=9.0, fees_usd=1.5)
    j.signal(ms, "A|BTCUSDT|1m|TREND", "BTCUSDT", "BUY", "PROBE", "NO_BET", "x" * 300)
    bot.lifecycle.ensure("A|BTCUSDT|1m|TREND", datetime.now(UTC))
    await bot.publish_snapshot()
    control_queue.enqueue(bot.db, bot.env, "risk_scale", {"value": 0.5})
    await bot.poll_controls()
    assert control_queue.recent(bot.db, bot.env)[0]["status"] == "done"
    assert kv_get(bot.db, bot.env, "risk_scale") == 0.5
    assert backend.snapshot(bot.db, bot.env)["bot_online"]
    assert backend.behavior(bot.db, bot.env)["n"] == 1
    assert backend.ledger(bot.db, bot.env)[0]["status"] == "RESEARCH"
    assert backend.discipline(bot.db, bot.env)["no_bet"] == {"x" * 64: 1}
    assert "BTCUSDT" in backend.export_csv(bot.db, bot.env, "trades")
