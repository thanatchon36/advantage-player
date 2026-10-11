from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import respx

from advantage_player import reports
from advantage_player.app import Bot
from advantage_player.config.settings import Secrets, StorageCfg, load_config
from advantage_player.ledger.journal import Journal
from advantage_player.notify.commands import build_commands
from advantage_player.notify.telegram import TelegramNotifier
from advantage_player.sim.fills import Leg, TradeResult

API = "https://tg.test"
CHAT = "-5322042654"


def _tg(**kw: float) -> TelegramNotifier:
    return TelegramNotifier(
        "TOKEN",
        CHAT,
        api=API,
        min_interval_s=kw.get("interval", 0.0),
        debounce_s=kw.get("debounce", 1800),
    )


def _texts(route: respx.Route) -> list[str]:
    return [parse_qs(c.request.content.decode())["text"][0] for c in route.calls]


@respx.mock
async def test_notify_is_nonblocking_and_sends_to_chat() -> None:
    route = respx.post(f"{API}/botTOKEN/sendMessage").respond(200, json={"ok": True})
    tg = _tg()
    await tg.start(poll_commands=False)
    t0 = time.perf_counter()
    tg.notify("open", "OPEN BTCUSDT")
    assert time.perf_counter() - t0 < 0.01  # enqueue only
    await asyncio.sleep(0.1)
    await tg.stop()
    assert _texts(route) == ["OPEN BTCUSDT"]
    assert parse_qs(route.calls[0].request.content.decode())["chat_id"] == [CHAT]


@respx.mock
async def test_backlog_is_coalesced_and_throttled() -> None:
    route = respx.post(f"{API}/botTOKEN/sendMessage").respond(200, json={"ok": True})
    tg = _tg(interval=0.2)
    for i in range(10):
        tg.notify("x", f"m{i}")
    await tg.start(poll_commands=False)
    await asyncio.sleep(0.15)
    await tg.stop()
    assert len(route.calls) == 1 and _texts(route)[0].count("\n\n") == 9


@respx.mock
async def test_429_is_retried() -> None:
    route = respx.post(f"{API}/botTOKEN/sendMessage")
    route.side_effect = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}}),
        httpx.Response(200, json={"ok": True}),
    ]
    tg = _tg()
    tg.notify("x", "hello")
    await tg.start(poll_commands=False)
    await asyncio.sleep(0.1)
    await tg.stop()
    assert _texts(route) == ["hello", "hello"]


@respx.mock
async def test_hanging_telegram_never_blocks_notify() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    respx.post(f"{API}/botTOKEN/sendMessage").mock(side_effect=hang)
    tg = _tg()
    await tg.start(poll_commands=False)
    t0 = time.perf_counter()
    for i in range(500):  # bounded queue: drops oldest, never raises or waits
        tg.notify("x", f"m{i}")
    assert time.perf_counter() - t0 < 0.1
    assert len(tg.queue) <= 200
    for t in tg._tasks:
        t.cancel()


async def test_tables_debounced_latest_wins() -> None:
    tg = _tg(debounce=0.2)
    tg.notify("tables", "A")
    tg.notify("tables", "B")
    tg.notify("tables", "C")
    assert list(tg.queue) == ["A"] and tg._pending_tables == "C"


async def test_disabled_without_token() -> None:
    tg = TelegramNotifier("", "")
    tg.notify("x", "y")
    assert not tg.queue


async def test_commands_only_from_configured_chat() -> None:
    tg = _tg()
    called: list[list[str]] = []

    async def h(args: list[str]) -> str:
        called.append(args)
        return "ok"

    tg.commands = {"kill": h}
    foreign = {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/kill"}}
    assert await tg.handle_update(foreign) is None and called == [] and tg.rejected_chats == 1
    mine = {"update_id": 2, "message": {"chat": {"id": int(CHAT)}, "text": "/kill@bot now"}}
    assert await tg.handle_update(mine) == "ok" and called == [["now"]]
    unknown = {"update_id": 3, "message": {"chat": {"id": int(CHAT)}, "text": "/moon"}}
    assert (await tg.handle_update(unknown) or "").startswith("commands:")


def _bot(tmp_path: Path) -> Bot:
    cfg = load_config("config.yaml").model_copy(
        update={"storage": StorageCfg(url=f"sqlite:///{tmp_path}/t.db", data_dir=str(tmp_path))}
    )
    return Bot(Secrets(_env_file=None), cfg)


async def test_command_set_matches_spec_and_risk_is_lower_only(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    cmds = build_commands(bot)
    assert set(cmds) == {
        "status",
        "pnl",
        "ledger",
        "tables",
        "pause",
        "resume",
        "close",
        "kill",
        "risk",
    }
    assert (await cmds["risk"](["scale", "2"])).startswith("refused")
    assert (await cmds["risk"](["set", "risk.max_risk_per_trade", "0.05"])).startswith("refused")
    assert "0.005" in await cmds["risk"](["set", "risk.max_risk_per_trade", "0.005"])
    assert bot.cfg.risk.max_risk_per_trade == 0.005
    assert (await cmds["pause"]([])).startswith("paused") and bot.discipline.state.manual_pause
    assert "mode: DEMO" in await cmds["status"]([])
    assert await cmds["tables"]([]) == "no scan yet"
    await bot.rest.aclose()


def test_daily_report_has_rake_vs_gross_and_bangkok_time(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    j = Journal(bot.db, "demo")
    ms = int(datetime(2026, 10, 10, 12, tzinfo=UTC).timestamp() * 1000)
    tr = TradeResult(
        "A|BTCUSDT|1m|TREND",
        "BTCUSDT",
        "BUY",
        "t",
        ms,
        ms,
        ms + 1,
        100,
        100,
        99,
        [Leg(101, 101, 1, 0, "target")],
        1,
        1.1,
        0.1,
        0,
        0,
        0,
        1,
        "target",
        False,
        None,
    )
    j.trade(tr, "PROBE", qty=1, pnl_usd=9.0, fees_usd=1.5, funding_usd=-0.5)
    j.trade(tr, "SHADOW")  # sims never count as real PnL
    j.signal(ms, "C|X|1m|RANGE", "X", "BUY", "SHADOW", "NO_BET", "status_SHADOW")
    text = reports.daily_report_text(
        bot.db, "demo", "DEMO", datetime(2026, 10, 10, tzinfo=UTC), 5000.0, 0.01
    )
    assert "Net PnL +9.00" in text and "gross +11.00" in text and "rake paid 2.00" in text
    assert "Bangkok" in text and "07:00 Bangkok" in text
    assert "A|BTCUSDT|1m|TREND 1" in text and "status_SHADOW 1" in text
