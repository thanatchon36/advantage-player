"""Bot orchestrator: market data → engine (SHADOW for every game) → lifecycle → execution.

Background loops: scanner (table selection), lifecycle evaluation, RESEARCH replay,
metadata refresh, clock sync. Execution of PROBE/LIVE games is attached via `executor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from advantage_player import reports
from advantage_player.config.changes import ChangeEvent, ConfigManager, Pending, apply_leaves
from advantage_player.config.settings import AppConfig, Secrets, load_config
from advantage_player.exchange.endpoints import ENDPOINTS
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.rest import BinanceRest
from advantage_player.execution.account import AccountTracker
from advantage_player.execution.executor import Executor as LiveExecutor
from advantage_player.execution.gateway import Gateway
from advantage_player.execution.userstream import UserStream
from advantage_player.ledger.journal import Journal
from advantage_player.ledger.lifecycle import GameStatus, Lifecycle, Transition
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.events import BookTicker, MarketEvent, MarkPrice
from advantage_player.marketdata.hub import MarketDataHub
from advantage_player.marketdata.recorder import TickRecorder
from advantage_player.notify.commands import build_commands
from advantage_player.notify.telegram import TelegramNotifier
from advantage_player.risk import control_queue
from advantage_player.risk.controls import Controls
from advantage_player.risk.discipline import Discipline, DisciplineState, entry_blockers
from advantage_player.scanner.persist import save_scan
from advantage_player.scanner.scanner import ScanResult, TableScanner
from advantage_player.sim.engine import GameEngine, SignalEvent
from advantage_player.sim.fills import Costs, TradeResult
from advantage_player.sim.replay import evaluate, fetch_klines, replay_bars
from advantage_player.storage.db import make_engine, record_config_version
from advantage_player.storage.kv import kv_get, kv_set

log = structlog.get_logger(__name__)
FALLBACK = Costs(maker_fee=0.0002, taker_fee=0.0005)


class Executor(Protocol):
    async def on_signal(self, ev: SignalEvent, status: GameStatus) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


Notify = Callable[[str, str], Awaitable[None] | None]  # (kind, text)


def now_utc() -> datetime:
    return datetime.now(UTC)


class Bot:
    def __init__(self, secrets: Secrets, cfg: AppConfig) -> None:
        secrets.check_mode()
        self.secrets = secrets
        self.cfg = cfg
        self.env = secrets.binance_env.value
        self.ep = ENDPOINTS[secrets.binance_env]
        self.db = make_engine(cfg.storage.url)
        record_config_version(self.db, cfg.hash, cfg.canonical(), "startup")
        self.rest = BinanceRest(
            self.ep.rest,
            secrets.api_key,
            secrets.api_secret,
            recv_window_ms=cfg.execution.recv_window_ms,
        )
        self.meta = ExchangeMetadata(self.rest)
        self.recorder = TickRecorder(cfg.storage.data_dir)
        self.engine = GameEngine(cfg, self.costs_for, self.tick_for)
        self.hub = MarketDataHub(self.ep.ws_stream, self.engine.timeframes, self.recorder)
        self.engine.state = self.hub.state
        self.hub.event_subscribers.append(self.engine.on_event)
        self.hub.bar_subscribers.append(self._on_bar)
        self.scanner = TableScanner(self.rest, self.meta, self.hub.state, cfg)
        self.lifecycle = Lifecycle(self.db, self.env, cfg.ledger, cfg.ledger.suspend_cooldown_h)
        self.journal = Journal(self.db, self.env)
        self.engine.signal_hooks.append(self._on_signal)
        self.engine.result_hooks.append(self._on_result)
        self.lifecycle.transition_hooks.append(self._on_transition)
        self.executor: Executor | None = None
        self.notifiers: list[Notify] = []
        self.scan: ScanResult | None = None
        self._stages: dict[
            tuple[str, int], tuple[str, str]
        ] = {}  # (game, signal_ms)→(stage, regime)
        self._tasks: list[asyncio.Task[None]] = []
        self._bg: set[asyncio.Task[None]] = set()
        self._warmed: set[str] = set()
        self._bars_1m: dict[str, deque[list[float]]] = {}  # dashboard charts
        self.started_ms = int(time.time() * 1000)
        self.stopping = asyncio.Event()
        self.config_path = "config.yaml"
        # discipline / controls state survives restarts
        self.discipline = Discipline(
            cfg.discipline, DisciplineState.from_json(kv_get(self.db, self.env, "discipline"))
        )
        self._risk_scale = float(kv_get(self.db, self.env, "risk_scale", 1.0))
        self.overrides: dict[str, Any] = dict(kv_get(self.db, self.env, "overrides", {}) or {})
        pend = [Pending(**p) for p in kv_get(self.db, self.env, "config_pending", []) or []]
        if self.overrides:
            self.cfg = cfg = apply_leaves(cfg, self.overrides)
        self.config_mgr = ConfigManager(cfg, pend)
        self.account = AccountTracker(self.db, self.env, cfg.risk, self.notify)
        self.account.on_halt = self._on_halt
        self.controls = Controls(self, lambda m: self.notify("control", m))
        self._last_funding: dict[str, int] = {}
        self._conn_ok = True
        self.gateway: Gateway | None = None
        self.live: LiveExecutor | None = None
        self.uds: UserStream | None = None
        self.telegram: TelegramNotifier | None = None
        tok = secrets.telegram_bot_token.get_secret_value()
        if tok and secrets.telegram_chat_id:
            self.telegram = TelegramNotifier(
                tok, secrets.telegram_chat_id, debounce_s=cfg.universe.table_change_debounce_s
            )
            self.notifiers.append(self.telegram.notify)
            self.telegram.commands = build_commands(self)
        if secrets.has_keys:
            self.gateway = Gateway(self.rest)
            self.live = LiveExecutor(self, self.gateway, self.env)
            self.executor = self.live
            self.uds = UserStream(self.rest, self.ep.ws_stream, self._on_user_event)
            self.uds.on_reconnect = self._on_uds_reconnect
            self.hub.event_subscribers.append(self._on_market_event)

    # ---- helpers ---------------------------------------------------------------------------
    @property
    def banner(self) -> str:
        return self.secrets.banner

    @property
    def risk_scale(self) -> float:
        return self._risk_scale

    @risk_scale.setter
    def risk_scale(self, value: float) -> None:
        """Persisted at once: a restart must never undo a lowered risk scale."""
        self._risk_scale = value
        kv_set(self.db, self.env, "risk_scale", value)

    def costs_for(self, symbol: str) -> Costs:
        f = self.meta.fees.get(symbol)
        m = self.scan.metrics.get(symbol) if self.scan else None
        return Costs(
            maker_fee=f.maker if f else FALLBACK.maker_fee,
            taker_fee=f.taker if f else FALLBACK.taker_fee,
            slippage_bps=self.cfg.universe.slippage_model_bps,
            spread_bps=m.spread_bps if m else 1.0,
            maker_timeout_ms=int(self.cfg.execution.maker_timeout_s * 1000),
        )

    def tick_for(self, symbol: str) -> float:
        si = self.meta.symbols.get(symbol)
        return float(si.tick_size) if si else 0.0

    async def notify(self, kind: str, text: str) -> None:
        for n in self.notifiers:
            try:
                r = n(kind, f"[{self.banner}] {text}")
                if r is not None:
                    await r
            except Exception as e:  # notifications must never break trading
                log.warning("notify_failed", error=repr(e))

    def _fire(self, coro: Coroutine[Any, Any, None]) -> None:
        """Fire-and-forget with a held reference (asyncio only keeps weak refs to tasks)."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no running loop (sync tests)
            coro.close()
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        async def guard() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task_crashed", task=name)
                await self.notify("error", f"task {name} crashed")

        self._tasks.append(asyncio.create_task(guard(), name=name))

    # ---- hooks -----------------------------------------------------------------------------
    def _on_bar(self, bar: Bar) -> None:
        self.engine.on_bar(bar)
        if self.live is not None:
            self._fire(self.live.on_bar(bar))
        if bar.tf_s == 60:
            self._publish_bar(bar)

    def _publish_bar(self, bar: Bar, write: bool = True) -> None:
        """Last 200 1m bars per open table / position symbol, for the dashboard chart."""
        q = self._bars_1m.setdefault(bar.symbol, deque(maxlen=200))
        q.append([bar.open_ms, bar.open, bar.high, bar.low, bar.close, bar.volume])
        if not write:
            return
        charted = set(self.scan.open if self.scan else []) | set(
            self.live.positions if self.live else []
        )
        if bar.symbol in charted:
            kv_set(self.db, self.env, f"bars:{bar.symbol}", list(q))

    def _on_market_event(self, ev: MarketEvent) -> None:
        if isinstance(ev, MarkPrice):
            prev = self.engine._next_funding.get(ev.symbol)
            if prev and ev.next_funding_ms > prev[0]:
                self._last_funding[ev.symbol] = prev[0]
            if self.live is not None and ev.symbol in self.live.positions:
                self._fire(self.live.on_mark(ev.symbol, ev.mark))

    def _on_signal(self, ev: SignalEvent) -> None:
        if ev.baseline:
            self._stages[(ev.game_id, ev.ts_ms)] = ("BASELINE", ev.regime.value)
            return
        status = self.lifecycle.ensure(ev.game_id, now_utc()).status
        sim_stage = (
            status.value
            if status in (GameStatus.RESEARCH, GameStatus.SHADOW)
            else (f"SIM_{status.value}")
        )
        self._stages[(ev.game_id, ev.ts_ms)] = (sim_stage, ev.regime.value)
        if status in (GameStatus.PROBE, GameStatus.LIVE) and self.executor is not None:
            self._fire(self.executor.on_signal(ev, status))
        else:
            reason = (
                "no executor"
                if status in (GameStatus.PROBE, GameStatus.LIVE)
                else (f"status_{status.value}")
            )
            self.journal.signal(
                ev.ts_ms,
                ev.game_id,
                ev.symbol,
                ev.signal.side,
                status.value,
                "NO_BET",
                reason,
                {"tag": ev.signal.tag},
            )

    def _on_result(self, r: TradeResult) -> None:
        stage, regime = self._stages.pop((r.game_id, r.signal_ms), ("RESEARCH", ""))
        self.journal.trade(r, stage, regime)

    def _on_transition(self, t: Transition) -> None:
        self._fire(
            self.notify("game", f"game {t.game_id}: {t.from_status} → {t.to_status} ({t.reason})")
        )

    # ---- ExecContext (used by the executor) ---------------------------------------------------
    def now_ms(self) -> int:
        return self.rest.clock.now_ms()

    def bankroll(self) -> float:
        return self.account.equity

    def drawdown(self) -> float:
        return self.account.snapshot().drawdown

    def daily_loss(self) -> float:
        return self.account.snapshot().daily_loss

    def cluster(self, symbol: str) -> int:
        return self.scan.cluster_of(symbol) if self.scan else -1

    def real_stats(self, game_id: str) -> tuple[float, float]:
        return self.lifecycle.real_stats(game_id)

    def book(self, symbol: str) -> BookTicker | None:
        st = self.hub.state.symbols.get(symbol)
        return st.book if st else None

    def mark(self, symbol: str) -> float | None:
        st = self.hub.state.symbols.get(symbol)
        return st.mark.mark if st and st.mark else None

    def costs(self, symbol: str) -> Costs:
        return self.costs_for(symbol)

    def blockers(self, symbol: str, regime: str) -> list[str]:
        now = self.now_ms()
        st = self.hub.state.symbols.get(symbol)
        d = self.cfg.discipline
        stale = self.hub.state.stale_reasons(
            symbol, int(time.time() * 1000), d.stale_data_s, ("book", "mark")
        )
        ws_ok = self.hub.streams.healthy and (self.uds is None or self.uds.connected)
        return entry_blockers(
            d,
            now_ms=now,
            stale=stale,
            ws_healthy=ws_ok,
            spread_bps=st.book.spread_bps if st and st.book else None,
            regime=regime,
            next_funding_ms=st.mark.next_funding_ms if st and st.mark else None,
            last_funding_ms=self._last_funding.get(symbol),
            clock_drift_ms=self.rest.clock.drift_ms,
            rate_headroom=self.rest.limiter.headroom(),
            allow_high_vol=self.cfg.regime.allow_high_vol,
        )

    async def edge_check(self, game_ids: list[str]) -> None:
        await self.evaluate_once()

    def persist_discipline(self) -> None:
        kv_set(self.db, self.env, "discipline", self.discipline.state.to_json())

    # ---- ControlTarget (Telegram / dashboard; risk-reducing only) -----------------------------
    def pause(self) -> None:
        self.discipline.pause()
        self.persist_discipline()

    def resume(self) -> str:
        res = self.discipline.resume(self.now_ms(), self.drawdown(), self.cfg.risk.dd_halt_at)
        self.persist_discipline()
        return res

    async def close(self, symbol: str | None) -> str:
        if self.live is None:
            return "no executor"
        return await (
            self.live.close_all("manual")
            if symbol is None
            else self.live.close(symbol.upper(), "manual")
        )

    async def kill(self, reason: str) -> None:
        if self.live is not None:
            await self.live.kill(reason)
        else:
            self.discipline.kill(self.now_ms())
            self.persist_discipline()

    def apply_effective(self, cfg: AppConfig, note: str) -> None:
        self.cfg = cfg
        self.config_mgr.effective = cfg
        self.engine.cfg = self.scanner.cfg = cfg
        self.discipline.cfg = cfg.discipline
        self.account.cfg = cfg.risk
        self.lifecycle.cfg = cfg.ledger
        kv_set(self.db, self.env, "overrides", self.overrides)
        kv_set(self.db, self.env, "risk_scale", self.risk_scale)
        record_config_version(self.db, cfg.hash, cfg.canonical(), note)

    async def _on_halt(self, why: str) -> None:
        await self.sync_transfers()  # a transfer out must never look like a drawdown
        if self.account.snapshot().dd_step < 0 and not self.discipline.state.halted:
            await self.kill(why)

    # ---- account / user stream ----------------------------------------------------------------
    async def refresh_account(self) -> None:
        if self.gateway is None:
            return
        acct = await self.gateway.account()
        open_risk = sum(r.f for r in self.live.open_risks()) if self.live else 0.0
        await self.account.update(float(acct["totalMarginBalance"]), self.now_ms(), open_risk)

    async def sync_transfers(self) -> None:
        if self.gateway is None:
            return
        now = self.now_ms()
        rows = await self.gateway.income("TRANSFER", now - 7 * 86_400_000, now)
        if self.account.apply_transfers(rows):
            await self.refresh_account()

    async def _on_user_event(self, msg: dict[str, Any]) -> None:
        if msg.get("e") == "ACCOUNT_UPDATE" and msg["a"].get("m") in ("WITHDRAW", "DEPOSIT"):
            self._fire(self.sync_transfers())
        if self.live is not None:
            await self.live.on_user_event(msg)

    async def _on_uds_reconnect(self) -> None:
        if self.live is not None:
            await self.live.reconcile()

    async def reconcile_once(self) -> None:
        if self.live is not None:
            notes = await self.live.reconcile()
            for n in notes:
                if "orphan" in n or "missing SL" in n:
                    await self.notify("risk", f"reconcile: {n}")

    async def watch_config(self) -> None:
        """Pre-commitment: file edits become immediate decreases or 24 h-queued increases."""
        try:
            desired = apply_leaves(load_config(self.config_path), self.overrides)
        except Exception as e:
            log.warning("config_invalid", error=repr(e))
            return
        dd = self.drawdown()
        events: list[ChangeEvent] = self.config_mgr.propose(desired, self.now_ms(), dd)
        events += self.config_mgr.tick(self.now_ms(), dd)
        kv_set(
            self.db,
            self.env,
            "config_pending",
            [p.__dict__ for p in self.config_mgr.pending.values()],
        )
        for ev in events:
            if ev.kind == "applied":
                self.apply_effective(self.config_mgr.effective, f"applied {list(ev.paths)}")
            self.journal.risk_event(f"config_{ev.kind}", paths=ev.paths, reason=ev.reason)
            await self.notify(
                "config", f"config {ev.kind}: {ev.paths} ({ev.reason}) → effective {self.cfg.hash}"
            )

    # ---- dashboard (reads only the DB) ---------------------------------------------------------
    async def publish_snapshot(self) -> None:
        kv_set(self.db, self.env, "live_snapshot", await self.live_snapshot())

    async def live_snapshot(self) -> dict[str, Any]:
        snap = self.account.snapshot()
        d = self.discipline.state
        now = self.now_ms()
        state = (
            "HALTED"
            if d.halted
            else "PAUSED"
            if d.manual_pause or d.paused_until_ms > now
            else "RUNNING"
        )
        positions = self.live.snapshot() if self.live else []
        if positions and self.gateway is not None:
            ex = {p["symbol"]: p for p in await self.gateway.positions()}
            for p in positions:
                e = ex.get(p["symbol"], {})
                p["liq"] = float(e.get("liquidationPrice") or 0)
                p["upnl"] = float(e.get("unRealizedProfit") or 0)
                p["mark"] = self.mark(p["symbol"])
                p["cluster"] = self.cluster(p["symbol"])
        risks = self.live.open_risks() if self.live else []
        by_cluster: dict[str, float] = {}
        for r in risks:
            by_cluster[str(r.cluster)] = by_cluster.get(str(r.cluster), 0.0) + r.f
        rc = self.cfg.risk
        return {
            "ts": now,
            "mode": self.banner,
            "env": self.env,
            "state": state,
            "status": self.status(),
            "account": {
                **snap.__dict__,
                "sweep_baseline": self.account.sweep_baseline,
                "sweep_trigger": rc.sweep_trigger,
            },
            "risk": {
                "open_risk": sum(r.f for r in risks),
                "long_risk": sum(r.f for r in risks if r.direction > 0),
                "short_risk": sum(r.f for r in risks if r.direction < 0),
                "by_cluster": by_cluster,
                "positions": len(risks),
                "risk_scale": self.risk_scale,
                "caps": {
                    "max_total_open_risk": rc.max_total_open_risk,
                    "max_cluster_risk": rc.max_cluster_risk,
                    "max_net_direction_risk": rc.max_net_direction_risk,
                    "max_positions": rc.max_positions,
                    "daily_loss_limit": rc.daily_loss_limit,
                    "dd_half_at": rc.dd_half_at,
                    "dd_halt_at": rc.dd_halt_at,
                },
            },
            "discipline": d.to_json(),
            "ws": {
                "market": self.hub.streams.healthy,
                "user": None if self.uds is None else self.uds.connected,
            },
            "positions": positions,
        }

    async def poll_controls(self) -> None:
        for note in await control_queue.process_pending(self.db, self.env, self.controls):
            log.info("dashboard_control", result=note)

    # ---- loops -----------------------------------------------------------------------------
    async def _every(
        self, seconds: float, fn: Callable[[], Awaitable[None]], name: str, delay: float = 0.0
    ) -> None:
        if delay:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)
        while not self.stopping.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("loop_error", loop=name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=seconds)

    async def scan_once(self) -> None:
        res = await self.scanner.scan()
        self.scan = res
        save_scan(self.db, res)
        symbols = [*res.open, *res.watch]
        await self._backfill([s for s in symbols if s not in self._warmed])
        await self.hub.set_symbols(symbols, [])
        if res.added or res.removed:
            await self.notify("tables", f"open tables +{res.added} −{res.removed} → {res.open}")

    async def _backfill(self, symbols: list[str]) -> None:
        """Warm minute-TF indicators from REST klines so regimes don't wait days."""
        end = int(time.time() * 1000)
        for sym in symbols:
            for tf, days in (("1m", 2), ("5m", 5)):
                if tf not in self.engine.timeframes:
                    continue
                bars = await fetch_klines(self.rest, sym, tf, end - days * 86_400_000, end)
                for b in bars:
                    self.engine.warm(b)
                if tf == "1m" and bars:
                    for b in bars[:-1]:
                        self._publish_bar(b, write=False)
                    self._publish_bar(bars[-1])
            self._warmed.add(sym)

    async def evaluate_once(self) -> None:
        eligible = set(self.scan.open) if self.scan else None
        regimes = {
            n.split("_", 1)[0]: {str(r) for r in g.regimes}
            for n, g in self.cfg.games.items()
            if g.enabled
        }
        for t in self.lifecycle.evaluate(now_utc(), eligible, regimes):
            log.info("transition", **t.__dict__)

    async def research_once(self) -> None:
        """RESEARCH: replay kline history for current tables (minute TFs), promote OOS passes."""
        if not self.scan:
            return
        end = int(time.time() * 1000)
        start = end - self.cfg.ledger.research_days * 86_400_000
        results: list[TradeResult] = []
        for sym in [*self.scan.open, *self.scan.watch]:
            eng = GameEngine(self.cfg, self.costs_for, bar_mode=True)
            eng.result_hooks.append(results.append)
            bars: list[Bar] = []
            for tf in sorted(t for t in eng.timeframes if t.endswith("m")):
                bars += await fetch_klines(self.rest, sym, tf, start, end)
            await asyncio.to_thread(replay_bars, eng, bars)
        split = start + int((end - start) * self.cfg.ledger.walk_forward_train_frac)
        evs = evaluate(results, split, self.cfg.ledger.bh_q, self.cfg.ledger.confidence)
        passed = [e.game_id for e in evs if e.passed]
        log.info("research_done", trades=len(results), games=len(evs), passed=len(passed))
        self.lifecycle.apply_research(passed, now_utc())

    async def refresh_meta(self) -> None:
        syms = [*self.scan.open, *self.scan.watch] if self.scan else ["BTCUSDT"]
        if self.secrets.has_keys:
            await self.meta.refresh_all(syms)
        else:
            await self.meta.refresh_exchange_info()

    async def sync_clock(self) -> None:
        await self.rest.sync_time()

    async def _daily_report_loop(self) -> None:
        """Daily report at 00:00 UTC for the UTC day that just ended (Bangkok time shown)."""
        while not self.stopping.is_set():
            now = datetime.now(UTC)
            nxt = reports.day_bounds(now)[1]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.stopping.wait(), timeout=(nxt - now).total_seconds() + 1
                )
            if self.stopping.is_set():
                return
            await self.send_daily_report(nxt - timedelta(days=1))

    async def send_daily_report(self, day: datetime) -> str:
        snap = self.account.snapshot()
        text = reports.daily_report_text(
            self.db, self.env, self.banner, day, snap.equity, snap.drawdown
        )
        if self.telegram is not None:
            self.telegram.notify("report", text)
        log.info("daily_report", day=f"{day:%Y-%m-%d}")
        return text

    async def check_connectivity(self) -> None:
        ws_ok = self.hub.streams.healthy
        uds_ok = self.uds is None or self.uds.connected
        ok = ws_ok and uds_ok
        if ok != self._conn_ok:
            self._conn_ok = ok
            if ok:
                await self.notify("conn", "connectivity restored")
            else:
                await self.notify(
                    "conn",
                    f"⚠️ connectivity problem: market WS "
                    f"{'ok' if ws_ok else 'DOWN'}, user stream "
                    f"{'ok' if uds_ok else 'DOWN'} (entries blocked)",
                )

    # ---- run -------------------------------------------------------------------------------
    async def start(self) -> None:
        log.info("bot_start", mode=self.banner, config=self.cfg.hash, rest=self.ep.rest)
        if self.telegram is not None:
            await self.telegram.start()
        await self.rest.sync_time()
        await self.refresh_meta()
        await self.hub.start()
        if self.gateway is not None:
            await self.refresh_account()
            await self.sync_transfers()
        await self.scan_once()
        if self.uds is not None:
            await self.uds.start()
        if self.executor:
            await self.executor.start()
        ex = self.cfg.execution
        if self.gateway is not None:
            self._spawn(self._every(10, self.refresh_account, "account", 10), "account")
            self._spawn(self._every(300, self.sync_transfers, "transfers", 300), "transfers")
            self._spawn(
                self._every(
                    ex.reconcile_interval_s,
                    self.reconcile_once,
                    "reconcile",
                    ex.reconcile_interval_s,
                ),
                "reconcile",
            )
        self._spawn(self._every(60, self.watch_config, "config", 60), "config")
        self._spawn(self._daily_report_loop(), "daily-report")
        self._spawn(self._every(15, self.check_connectivity, "connectivity", 30), "connectivity")
        u, lg = self.cfg.universe, self.cfg.ledger
        self._spawn(
            self._every(u.scan_interval_s, self.scan_once, "scan", u.scan_interval_s), "scan"
        )
        self._spawn(self._every(lg.eval_interval_s, self.evaluate_once, "eval"), "eval")
        self._spawn(
            self._every(lg.research_interval_h * 3600, self.research_once, "research"), "research"
        )
        self._spawn(self._every(86_400, self.refresh_meta, "meta", 86_400), "meta")
        self._spawn(self._every(600, self.sync_clock, "clock", 600), "clock")
        self._spawn(self._every(5, self.publish_snapshot, "snapshot"), "snapshot")
        self._spawn(self._every(2, self.poll_controls, "controls"), "controls")
        await self.notify("start", f"bot started, config {self.cfg.hash}")

    async def stop(self) -> None:
        """Graceful: executor cancels pending entries but keeps exchange-side SLs."""
        self.stopping.set()
        if self.executor:
            await self.executor.stop()
        if self.uds is not None:
            await self.uds.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.hub.stop()
        await self.notify("stop", f"bot stopped, config {self.cfg.hash}")
        if self.telegram is not None:
            await self.telegram.stop()
        await self.rest.aclose()

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for g in self.lifecycle.games.values():
            counts[g.status.value] = counts.get(g.status.value, 0) + 1
        return {
            "mode": self.banner,
            "config": self.cfg.hash,
            "open_tables": self.scan.open if self.scan else [],
            "watch": self.scan.watch if self.scan else [],
            "ws_healthy": self.hub.streams.healthy,
            "events": dict(self.hub.counts),
            "games": counts,
            "open_sim_trades": len(self.engine.open),
            "clock_drift_ms": round(self.rest.clock.drift_ms),
            "rate_headroom": round(self.rest.limiter.headroom(), 3),
            "equity": round(self.account.equity, 2),
            "drawdown": round(self.account.snapshot().drawdown, 4),
            "positions": list(self.live.positions) if self.live else [],
            "halted": self.discipline.state.halted,
            "paused": self.discipline.state.manual_pause,
            "uds": bool(self.uds and self.uds.connected),
        }
