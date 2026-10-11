"""Bot orchestrator: market data → engine (SHADOW for every game) → lifecycle → execution.

Background loops: scanner (table selection), lifecycle evaluation, RESEARCH replay,
metadata refresh, clock sync. Execution of PROBE/LIVE games is attached via `executor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from advantage_player.config.settings import AppConfig, Secrets
from advantage_player.exchange.endpoints import ENDPOINTS
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.rest import BinanceRest
from advantage_player.ledger.journal import Journal
from advantage_player.ledger.lifecycle import GameStatus, Lifecycle, Transition
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.hub import MarketDataHub
from advantage_player.marketdata.recorder import TickRecorder
from advantage_player.scanner.persist import save_scan
from advantage_player.scanner.scanner import ScanResult, TableScanner
from advantage_player.sim.engine import GameEngine, SignalEvent
from advantage_player.sim.fills import Costs, TradeResult
from advantage_player.sim.replay import evaluate, fetch_klines, replay_bars
from advantage_player.storage.db import make_engine, record_config_version

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
        self.started_ms = int(time.time() * 1000)
        self.stopping = asyncio.Event()

    # ---- helpers ---------------------------------------------------------------------------
    @property
    def banner(self) -> str:
        return self.secrets.banner

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

    # ---- run -------------------------------------------------------------------------------
    async def start(self) -> None:
        log.info("bot_start", mode=self.banner, config=self.cfg.hash, rest=self.ep.rest)
        await self.rest.sync_time()
        await self.refresh_meta()
        await self.hub.start()
        await self.scan_once()
        if self.executor:
            await self.executor.start()
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
        await self.notify("start", f"bot started, config {self.cfg.hash}")

    async def stop(self) -> None:
        """Graceful: executor cancels pending entries but keeps exchange-side SLs."""
        self.stopping.set()
        if self.executor:
            await self.executor.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.hub.stop()
        await self.notify("stop", "bot stopped")
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
        }
