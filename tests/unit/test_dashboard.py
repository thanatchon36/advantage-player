from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from advantage_player.app import Bot
from advantage_player.config.settings import Secrets, StorageCfg, load_config
from advantage_player.dashboard import backend
from advantage_player.dashboard.server import COOKIE, _sign, create_app
from advantage_player.ledger.journal import Journal
from advantage_player.marketdata.bars import Bar
from advantage_player.risk import control_queue
from advantage_player.scanner.scanner import ScanResult
from advantage_player.sim.fills import Leg, TradeResult
from advantage_player.storage.kv import kv_get
from advantage_player.storage.models import ControlRequest

PW = "s3cret"


def _bot(tmp_path: Path) -> Bot:
    cfg = load_config("config.yaml").model_copy(
        update={"storage": StorageCfg(url=f"sqlite:///{tmp_path}/t.db", data_dir=str(tmp_path))}
    )
    return Bot(Secrets(_env_file=None), cfg)


def _client(bot: Bot, login: bool = True) -> TestClient:
    c = TestClient(create_app(bot.db, bot.env, PW))
    if login:
        r = c.post("/login", data={"password": PW}, follow_redirects=False)
        assert r.status_code == 303 and COOKIE in r.cookies
    return c


def _trade(bot: Bot, stage: str, r_net_exit: float = 101.0) -> None:
    ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    tr = TradeResult(
        "A|BTCUSDT|1m|TREND",
        "BTCUSDT",
        "BUY",
        "t",
        ms,
        ms,
        ms + 60_000,
        100,
        100,
        99,
        [Leg(r_net_exit, r_net_exit, 1, 0, "target")],
        1,
        1.1,
        0.1,
        0,
        0,
        -0.3,
        1.2,
        "target",
        False,
        None,
    )
    Journal(bot.db, bot.env).trade(tr, stage, "TREND", qty=1, pnl_usd=9.0, fees_usd=1.5)


def test_refuses_to_start_without_password(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        create_app(_bot(tmp_path).db, "demo", "")


def test_auth_required_everywhere(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    c = _client(bot, login=False)
    for path in (
        "/api/snapshot",
        "/api/tables",
        "/api/ledger",
        "/api/game/x",
        "/api/bankroll",
        "/api/chart/BTCUSDT",
        "/api/behavior",
        "/api/discipline",
        "/api/export/trades.csv",
    ):
        assert c.get(path).status_code == 401, path
    assert c.post("/api/control", json={"action": "pause"}).status_code == 401
    assert "password" in c.get("/").text  # login form, not the app
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    c.cookies.set(COOKIE, "9999999999.forged")
    assert c.get("/api/snapshot").status_code == 401
    key_other = b"x" * 32
    c.cookies.set(COOKIE, _sign(key_other, int(time.time()) + 100))
    assert c.get("/api/snapshot").status_code == 401
    with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect("/ws") as ws:
        ws.receive_json()
    assert e.value.code == 4401
    c = _client(bot)
    assert c.get("/api/snapshot").status_code == 200
    assert "lightweight-charts@" in c.get("/").text  # pinned CDN version


async def test_snapshot_shape_and_ws_push(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await bot.publish_snapshot()
    c = _client(bot)
    s = c.get("/api/snapshot").json()
    assert s["bot_online"] is True and s["mode"] == "DEMO" and s["state"] == "RUNNING"
    for k in ("account", "risk", "ws", "positions", "discipline", "today", "status"):
        assert k in s, k
    assert {"equity", "drawdown", "dd_step", "daily_loss", "sweep_baseline"} <= set(s["account"])
    caps = s["risk"]["caps"]
    assert caps["max_total_open_risk"] == 0.03 and caps["max_positions"] == 4
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["mode"] == "DEMO"


async def test_controls_only_reduce_risk(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    c = _client(bot)
    for bad in ("resume", "increase_risk", "set_leverage"):
        assert c.post("/api/control", json={"action": bad}).status_code == 400
    ok = [
        {"action": "risk_scale", "args": {"value": 1.5}},  # increase → refused by Controls
        {"action": "lower", "args": {"path": "target_leverage", "value": 100}},  # increase
        {"action": "lower", "args": {"path": "risk.max_risk_per_trade", "value": 0.005}},
        {"action": "risk_scale", "args": {"value": 0.5}},
        {"action": "pause"},
    ]
    for body in ok:
        assert c.post("/api/control", json=body).json()["status"] == "pending"
    await bot.poll_controls()
    rows = {r["id"]: r for r in control_queue.recent(bot.db, bot.env)}
    assert [rows[i]["status"] for i in sorted(rows)] == [
        "refused",
        "refused",
        "done",
        "done",
        "done",
    ]
    assert bot.cfg.target_leverage == 50
    assert bot.cfg.risk.max_risk_per_trade == 0.005 and bot.risk_scale == 0.5
    assert bot.discipline.state.manual_pause
    assert kv_get(bot.db, bot.env, "overrides") == {"risk.max_risk_per_trade": 0.005}
    restarted = _bot(tmp_path)  # lowered risk survives a restart
    assert restarted.risk_scale == 0.5 and restarted.cfg.risk.max_risk_per_trade == 0.005


async def test_stale_control_requests_expire(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    rid = control_queue.enqueue(bot.db, bot.env, "pause", {})
    with Session(bot.db) as s:
        row = s.get(ControlRequest, rid)
        assert row is not None
        row.ts = datetime.now(UTC) - timedelta(minutes=5)
        s.commit()
    await bot.poll_controls()
    assert control_queue.recent(bot.db, bot.env)[0]["status"] == "expired"
    assert not bot.discipline.state.manual_pause


def test_csv_export(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    _trade(bot, "PROBE")
    c = _client(bot)
    r = c.get("/api/export/trades.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,env,stage,game_id") and len(lines) == 2
    assert "+00:00" in lines[1]  # UTC timestamps
    for kind in backend.EXPORTS:
        assert c.get(f"/api/export/{kind}.csv").status_code == 200
    assert c.get("/api/export/secrets.csv").status_code == 404


def test_views_over_seeded_data(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    _trade(bot, "PROBE")
    _trade(bot, "SHADOW", 98.0)
    bot.lifecycle.ensure("A|BTCUSDT|1m|TREND", datetime.now(UTC))
    c = _client(bot)
    led = c.get("/api/ledger").json()
    assert led[0]["game"] == "A|BTCUSDT|1m|TREND" and led[0]["status"] == "RESEARCH"
    g = c.get("/api/game/A|BTCUSDT|1m|TREND").json()
    assert set(g["curves"]) == {"PROBE", "SHADOW"} and len(g["r"]) == 2
    real = c.get("/api/behavior").json()
    assert real["n"] == 1 and real["groups"]["side"]["BUY"]["usd"] == 9.0
    assert set(real["groups"]["hour_bkk"]) and real["mae_mfe"][0][0] == -0.3
    assert set(real["slippage_by_stage"]) == {"SHADOW", "PROBE"}
    assert c.get("/api/behavior?scope=shadow").json()["n"] == 1
    d = c.get("/api/discipline").json()
    assert d["config_versions"] and "no_bet" in d and "pending" in d
    assert c.get("/api/tables").json() == {"ts": None, "rows": []}
    assert c.get("/api/chart/BTCUSDT").json()["trades"][0]["r"] == pytest.approx(1.0)


def test_bars_published_for_open_tables_only(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot.scan = ScanResult(0, {}, {}, {}, ["BTCUSDT"], ["ETHUSDT"])
    for i in range(205):
        for sym in ("BTCUSDT", "ETHUSDT"):
            bot._publish_bar(Bar(sym, 60, i * 60_000, 1, 2, 0.5, 1.5, 10))
    bars = kv_get(bot.db, bot.env, "bars:BTCUSDT")
    assert len(bars) == 200 and bars[-1][0] == 204 * 60_000
    assert kv_get(bot.db, bot.env, "bars:ETHUSDT") is None
    c = _client(bot)
    assert len(c.get("/api/chart/btcusdt").json()["bars"]) == 200
