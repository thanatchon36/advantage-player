"""Executes PROBE / LIVE games on the exchange (SPEC §6–8).

Per position: decision → (maker-first | taker) entry → exchange-side SL (STOP_MARKET,
closePosition, MARK_PRICE, via algoOrder) confirmed within 2 s of the fill, else flatten →
TPs → BE / trailing via make-before-break SL replacement → time stop → finalize from
authoritative fills (userTrades + FUNDING_FEE income) → R-multiple into the ledger.

Rules enforced here: one position per symbol (no adding), SL never widened or removed while
open, graceful stop keeps SLs, kill flattens everything, state persisted for restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import zlib
from collections.abc import Coroutine
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import structlog

from advantage_player.config.settings import AppConfig
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.models import SymbolInfo
from advantage_player.exchange.rest import new_client_id
from advantage_player.execution.gateway import Gateway, OrderRejectedError
from advantage_player.ledger.journal import Journal
from advantage_player.ledger.lifecycle import GameStatus
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.events import BookTicker
from advantage_player.risk.caps import OpenRisk
from advantage_player.risk.discipline import Discipline, validate_sl_move
from advantage_player.risk.manager import Decision, EntryRequest, decide
from advantage_player.risk.sizing import liq_guard_ok
from advantage_player.sim.engine import SignalEvent, _tf_name
from advantage_player.sim.fills import Costs, Leg, TradeResult
from advantage_player.storage.kv import kv_get, kv_set

log = structlog.get_logger(__name__)


class ExecContext(Protocol):
    cfg: AppConfig
    meta: ExchangeMetadata
    journal: Journal
    discipline: Discipline
    risk_scale: float

    def now_ms(self) -> int: ...
    def bankroll(self) -> float: ...
    def drawdown(self) -> float: ...
    def daily_loss(self) -> float: ...
    def blockers(self, symbol: str, regime: str) -> list[str]: ...
    def cluster(self, symbol: str) -> int: ...
    def real_stats(self, game_id: str) -> tuple[float, float]: ...
    def book(self, symbol: str) -> BookTicker | None: ...
    def mark(self, symbol: str) -> float | None: ...
    def costs(self, symbol: str) -> Costs: ...
    async def notify(self, kind: str, text: str) -> None: ...
    async def edge_check(self, game_ids: list[str]) -> None: ...
    def persist_discipline(self) -> None: ...


def _cid(prefix: str, *parts: object) -> str:
    """Deterministic idempotency key: same signal ⇒ same id (never double-enter on restart)."""
    h = zlib.crc32("|".join(map(str, parts)).encode())
    return f"ap_{prefix}_{h:08x}_{parts[-1]}"[:36]


@dataclass
class ManagedPosition:
    symbol: str
    game_id: str
    status: str
    side: str
    direction: int
    regime: str
    tf: str
    tag: str
    signal_ms: int
    entry_ref: float
    stop_initial: float
    stop: float
    targets: list[tuple[float, float, bool]]  # (R, fraction, maker)
    be_after_first: bool
    trail_atr: float
    atr: float
    time_stop_bars: int
    entry_type: str
    entry_client_id: str
    f: float
    leverage: int
    state: str = "entering"  # entering | open | closing | closed
    filled_qty: float = 0.0
    entry_avg: float = 0.0
    entry_ms: int = 0
    exit_qty: float = 0.0
    sl_id: str = ""
    sl_close_position: bool = True
    tp_ids: list[str] = field(default_factory=list)
    tp_hit: int = 0
    bars_held: int = 0
    extreme: float = 0.0  # most favourable mark since entry
    adverse: float = 0.0  # least favourable mark since entry
    last_trail_ms: int = 0
    close_ref: float = 0.0
    close_reason: str = ""
    model_entry_slip_bps: float = 0.0

    def risk_usd(self) -> float:
        return self.filled_qty * abs(self.entry_ref - self.stop_initial)

    def target_price(self, r: float) -> float:
        return self.entry_ref + self.direction * r * abs(self.entry_ref - self.stop_initial)

    @property
    def exit_side(self) -> str:
        return "SELL" if self.direction > 0 else "BUY"


class Executor:
    def __init__(self, ctx: ExecContext, gateway: Gateway, env: str) -> None:
        self.ctx = ctx
        self.gw = gateway
        self.env = env
        self.positions: dict[str, ManagedPosition] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._fill_events: dict[str, asyncio.Event] = {}
        self.stopping = False
        self._bg: set[asyncio.Task[None]] = set()
        self._retiring: set[str] = set()  # SL ids we are cancelling on purpose (moves)
        self._load()

    def _fire(self, coro: Coroutine[Any, Any, None]) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ---- persistence -----------------------------------------------------------------------
    def _load(self) -> None:
        raw = kv_get(self.ctx.journal.db, self.env, "positions", {}) or {}
        for sym, d in raw.items():
            d["targets"] = [tuple(t) for t in d["targets"]]
            self.positions[sym] = ManagedPosition(**d)

    def _save(self) -> None:
        kv_set(
            self.ctx.journal.db,
            self.env,
            "positions",
            {s: asdict(p) for s, p in self.positions.items() if p.state != "closed"},
        )

    def _lock(self, symbol: str) -> asyncio.Lock:
        return self._locks.setdefault(symbol, asyncio.Lock())

    def open_risks(self) -> list[OpenRisk]:
        return [
            OpenRisk(p.symbol, p.direction, p.f, self.ctx.cluster(p.symbol))
            for p in self.positions.values()
            if p.state != "closed"
        ]

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        await self.gw.ensure_one_way()
        await self.reconcile()

    async def stop(self) -> None:
        """Graceful: cancel pending entries, KEEP exchange-side SLs / TPs."""
        self.stopping = True
        for p in list(self.positions.values()):
            if p.state == "entering":
                await self.gw.cancel_order(p.symbol, p.entry_client_id)
        self._save()

    # ---- signals ---------------------------------------------------------------------------
    async def on_signal(self, ev: SignalEvent, status: GameStatus) -> None:
        if self.stopping:
            return
        async with self._lock(ev.symbol):
            dec = self._decide(ev, status)
            detail = {
                "tag": ev.signal.tag,
                "f": dec.f,
                "qty": dec.qty,
                "lev": dec.leverage,
                "mode": dec.mode,
            }
            self.ctx.journal.signal(
                ev.ts_ms,
                ev.game_id,
                ev.symbol,
                ev.signal.side,
                status.value,
                "BET" if dec.bet else "NO_BET",
                dec.reason,
                detail,
            )
            if dec.bet:
                try:
                    await self._enter(ev, status, dec)
                except Exception as e:
                    log.exception("enter_failed", symbol=ev.symbol)
                    await self.ctx.notify("error", f"entry failed {ev.symbol}: {e!r}")
                    await self._emergency_flatten(ev.symbol, f"entry error {e!r}")

    def _decide(self, ev: SignalEvent, status: GameStatus) -> Decision:
        c = self.ctx
        mu_low, e_r2 = c.real_stats(ev.game_id)
        req = EntryRequest(
            ev.symbol,
            ev.signal.direction,
            ev.signal.entry_ref,
            ev.signal.stop,
            status,
            c.cluster(ev.symbol),
            mu_low,
            e_r2,
        )
        return decide(
            req,
            c.cfg,
            c.discipline,
            now_ms=c.now_ms(),
            bankroll=c.bankroll(),
            drawdown=c.drawdown(),
            daily_loss_frac=c.daily_loss(),
            open_risks=self.open_risks(),
            blockers=c.blockers(ev.symbol, ev.regime.value),
            si=c.meta.symbols.get(ev.symbol),
            brackets=c.meta.brackets.get(ev.symbol),
            risk_scale=c.risk_scale,
        )

    # ---- entry -----------------------------------------------------------------------------
    async def _enter(self, ev: SignalEvent, status: GameStatus, dec: Decision) -> None:
        sig = ev.signal
        si = self.ctx.meta.symbols[ev.symbol]
        cid = _cid("e", ev.game_id, ev.ts_ms)
        if ev.symbol in self.positions:
            return
        await self.gw.ensure_symbol(ev.symbol, dec.leverage)
        pos = ManagedPosition(
            symbol=ev.symbol,
            game_id=ev.game_id,
            status=status.value,
            side=sig.side,
            direction=sig.direction,
            regime=ev.regime.value,
            tf=ev.tf,
            tag=sig.tag,
            signal_ms=ev.ts_ms,
            entry_ref=sig.entry_ref,
            stop_initial=sig.stop,
            stop=sig.stop,
            targets=[(t.r, t.fraction, t.maker) for t in sig.targets],
            be_after_first=sig.move_stop_to_be_after_first_target,
            trail_atr=sig.trail_atr,
            atr=sig.atr,
            time_stop_bars=sig.time_stop_bars,
            entry_type=sig.entry_type,
            entry_client_id=cid,
            f=dec.f,
            leverage=dec.leverage,
        )
        self.positions[ev.symbol] = pos
        self._save()
        qty = si.round_qty(dec.qty)
        filled, avg = Decimal(0), 0.0
        if sig.entry_type == "maker":
            filled, avg = await self._maker_entry(pos, si, qty)
            if filled == 0:
                if not self._taker_ok(pos):
                    return self._abandon(pos, "maker_timeout")
                pos.entry_client_id = cid = _cid("t", ev.game_id, ev.ts_ms)
        if filled == 0:
            if not self._taker_ok(pos):
                return self._abandon(pos, "slippage_cap")
            costs = self.ctx.costs(ev.symbol)
            pos.model_entry_slip_bps = costs.spread_bps / 2 + costs.slippage_bps
            try:
                r = await self.gw.place_order(si, sig.side, "MARKET", qty, cid)
            except OrderRejectedError as e:
                return self._abandon(pos, f"entry_rejected:{e.code}")
            filled, avg = Decimal(str(r.get("executedQty", "0"))), float(r.get("avgPrice") or 0)
            if filled == 0 or avg <= 0:  # DEMO may answer RESULT with avgPrice 0
                got = await self.gw.get_order(si.symbol, cid)
                if got:
                    filled, avg = Decimal(got["executedQty"]), float(got["avgPrice"] or 0)
        if filled == 0:
            return self._abandon(pos, "not_filled")
        if avg <= 0:
            for p in await self.gw.positions():
                if p["symbol"] == pos.symbol:
                    avg = float(p["entryPrice"])
        fill_ms = self.ctx.now_ms()
        pos.filled_qty, pos.entry_avg, pos.entry_ms = float(filled), avg, fill_ms
        pos.extreme = pos.adverse = avg
        pos.state = "open"
        self._save()
        await self._protect(pos, si, fill_ms)
        if pos.state == "open":
            await self._place_tps(pos, si)
            await self._liq_check(pos)
        if pos.state == "open":
            await self._notify_open(pos)

    def _taker_ok(self, pos: ManagedPosition) -> bool:
        """Taker fallback only if the signal is still valid and expected slippage < cap."""
        bt = self.ctx.book(pos.symbol)
        if bt is None:
            return False
        px = bt.ask if pos.direction > 0 else bt.bid
        slip_bps = pos.direction * (px - pos.entry_ref) / pos.entry_ref * 1e4
        stop_ok = (bt.bid > pos.stop) if pos.direction > 0 else (bt.ask < pos.stop)
        return stop_ok and slip_bps <= self.ctx.cfg.execution.max_taker_slippage_bps

    async def _maker_entry(
        self, pos: ManagedPosition, si: SymbolInfo, qty: Decimal
    ) -> tuple[Decimal, float]:
        price = si.round_price(pos.entry_ref, "down" if pos.direction > 0 else "up")
        ev = self._fill_events.setdefault(pos.entry_client_id, asyncio.Event())
        try:
            await self.gw.place_order(
                si, pos.side, "LIMIT", qty, pos.entry_client_id, price=price, tif="GTX"
            )
        except OrderRejectedError as e:
            log.info("maker_rejected", symbol=pos.symbol, code=e.code)  # -5022 would cross
            return Decimal(0), 0.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout=self.ctx.cfg.execution.maker_timeout_s)
        await self.gw.cancel_order(pos.symbol, pos.entry_client_id)
        got = await self.gw.get_order(pos.symbol, pos.entry_client_id)
        self._fill_events.pop(pos.entry_client_id, None)
        if not got:
            return Decimal(0), 0.0
        return Decimal(got["executedQty"]), float(got.get("avgPrice") or 0)

    def _abandon(self, pos: ManagedPosition, reason: str) -> None:
        pos.state = "closed"
        self.positions.pop(pos.symbol, None)
        self._save()
        self.ctx.journal.signal(
            pos.signal_ms,
            pos.game_id,
            pos.symbol,
            pos.side,
            pos.status,
            "NO_BET",
            reason,
            {"stage": "entry"},
        )

    # ---- protection ------------------------------------------------------------------------
    async def _protect(self, pos: ManagedPosition, si: SymbolInfo, fill_ms: int) -> None:
        """Rule 3: SL confirmed within sl_confirm_timeout_s of the fill, else flatten + alert."""
        timeout = self.ctx.cfg.discipline.sl_confirm_timeout_s
        stop = si.round_price(pos.stop, "up" if pos.direction > 0 else "down")
        mark = self.ctx.mark(pos.symbol)
        if mark is not None and (mark - float(stop)) * pos.direction <= 0:
            await self._emergency_flatten(pos.symbol, "price already through stop at fill")
            return
        sl_id = _cid("sl", pos.game_id, pos.signal_ms)
        try:
            remaining = max(timeout - (self.ctx.now_ms() - fill_ms) / 1000, 0.1)
            r = await asyncio.wait_for(
                self.gw.place_algo(
                    si, pos.exit_side, "STOP_MARKET", stop, sl_id, close_position=True
                ),
                timeout=remaining,
            )
            ok = r.get("algoStatus") == "NEW"
        except (TimeoutError, OrderRejectedError) as e:
            ok = False
            log.error("sl_place_failed", symbol=pos.symbol, error=repr(e))
        latency = self.ctx.now_ms() - fill_ms
        if not ok or latency > timeout * 1000:
            self.ctx.journal.risk_event("sl_failure", pos.symbol, latency_ms=latency)
            await self._emergency_flatten(pos.symbol, f"SL not confirmed in {timeout:g}s")
            return
        pos.sl_id, pos.stop, pos.sl_close_position = sl_id, float(stop), True
        self._save()

    async def _place_tps(self, pos: ManagedPosition, si: SymbolInfo) -> None:
        n = len(pos.targets)
        for i, (r, frac, maker) in enumerate(pos.targets):
            price = si.round_price(pos.target_price(r), "nearest")
            tid = _cid(f"tp{i}", pos.game_id, pos.signal_ms)
            last = i == n - 1
            qty = si.round_qty(pos.filled_qty * frac)
            try:
                if maker:
                    q = (
                        si.round_qty(
                            pos.filled_qty
                            - sum(
                                float(si.round_qty(pos.filled_qty * f))
                                for _, f, _ in pos.targets[:i]
                            )
                        )
                        if last
                        else qty
                    )
                    await self.gw.place_order(
                        si, pos.exit_side, "LIMIT", q, tid, price=price, tif="GTX", reduce_only=True
                    )
                elif last:
                    await self.gw.place_algo(
                        si, pos.exit_side, "TAKE_PROFIT_MARKET", price, tid, close_position=True
                    )
                else:
                    if qty <= 0:
                        continue
                    await self.gw.place_algo(
                        si, pos.exit_side, "TAKE_PROFIT_MARKET", price, tid, qty=qty
                    )
                pos.tp_ids.append(tid)
            except OrderRejectedError as e:
                log.warning("tp_place_failed", symbol=pos.symbol, i=i, code=e.code, msg=e.msg)
        self._save()

    async def _liq_check(self, pos: ManagedPosition) -> None:
        for p in await self.gw.positions():
            if p["symbol"] == pos.symbol:
                liq = float(p.get("liquidationPrice") or 0)
                if not liq_guard_ok(pos.entry_avg, pos.stop, liq, self.ctx.cfg.risk):
                    self.ctx.journal.risk_event("liq_guard", pos.symbol, liq=liq, stop=pos.stop)
                    await self._emergency_flatten(pos.symbol, "stop beyond 60% of liq distance")

    async def move_sl(self, pos: ManagedPosition, new_stop: float, why: str) -> bool:
        """Rule 4 via make-before-break: never a moment without a stop on the exchange."""
        si = self.ctx.meta.symbols[pos.symbol]
        stop = si.round_price(new_stop, "down" if pos.direction > 0 else "up")
        validate_sl_move(pos.direction, pos.stop, float(stop))
        if float(stop) == pos.stop:
            return False
        remaining = si.round_qty(pos.filled_qty - pos.exit_qty)
        if remaining <= 0:
            return False
        tmp = new_client_id("slt")
        try:
            await self.gw.place_algo(si, pos.exit_side, "STOP_MARKET", stop, tmp, qty=remaining)
        except OrderRejectedError as e:
            log.info("sl_move_skipped", symbol=pos.symbol, code=e.code, why=why)
            return False
        old = pos.sl_id
        self._retiring.add(old)
        await self.gw.cancel_algo(old)
        new = new_client_id("sl")
        try:
            await self.gw.place_algo(
                si, pos.exit_side, "STOP_MARKET", stop, new, close_position=True
            )
            self._retiring.add(tmp)
            await self.gw.cancel_algo(tmp)
            pos.sl_id, pos.sl_close_position = new, True
        except OrderRejectedError:
            pos.sl_id, pos.sl_close_position = tmp, False  # qty-based SL stays as protection
        pos.stop = float(stop)
        self._save()
        log.info("sl_moved", symbol=pos.symbol, stop=pos.stop, why=why)
        return True

    # ---- market feeds ----------------------------------------------------------------------
    async def on_mark(self, symbol: str, mark: float) -> None:
        pos = self.positions.get(symbol)
        if not pos or pos.state != "open":
            return
        pos.extreme = max(pos.extreme, mark) if pos.direction > 0 else min(pos.extreme, mark)
        pos.adverse = min(pos.adverse, mark) if pos.direction > 0 else max(pos.adverse, mark)
        if pos.tp_hit >= 1 and pos.trail_atr > 0 and pos.atr > 0:
            now = self.ctx.now_ms()
            trail = pos.extreme - pos.direction * pos.trail_atr * pos.atr
            if (
                trail - pos.stop
            ) * pos.direction >= 0.1 * pos.atr and now - pos.last_trail_ms > 5000:
                pos.last_trail_ms = now
                async with self._lock(symbol):
                    await self.move_sl(pos, trail, "trail")

    async def on_bar(self, bar: Bar) -> None:
        pos = self.positions.get(bar.symbol)
        if not pos or pos.state != "open" or _tf_name(bar.tf_s) != pos.tf:
            return
        pos.bars_held += 1
        self._save()
        if pos.time_stop_bars and pos.bars_held >= pos.time_stop_bars:
            await self.close(bar.symbol, "time", bar.close)

    # ---- user data stream ------------------------------------------------------------------
    async def on_user_event(self, msg: dict[str, Any]) -> None:
        e = msg.get("e")
        if e == "ORDER_TRADE_UPDATE":
            o = msg["o"]
            cid = o.get("c", "")
            if cid in self._fill_events and o.get("X") in ("FILLED", "CANCELED", "EXPIRED"):
                self._fill_events[cid].set()
            pos = self.positions.get(o["s"])
            if (
                pos
                and pos.state in ("open", "closing")
                and o.get("x") == "TRADE"
                and (o.get("S") == pos.exit_side)
            ):
                pos.exit_qty += float(o.get("l", 0))
                await self._after_exit_fill(pos, o)
        elif e == "ALGO_UPDATE":
            o = msg["o"]
            pos = self.positions.get(o["s"])
            caid = o.get("caid", "")
            if caid in self._retiring:  # we cancelled it on purpose (SL move)
                self._retiring.discard(caid)
            elif (
                pos
                and caid == pos.sl_id
                and o.get("X") in ("CANCELED", "EXPIRED", "REJECTED")
                and pos.state == "open"
                and pos.exit_qty < pos.filled_qty
            ):
                log.error("sl_lost", symbol=pos.symbol, status=o.get("X"))
                await self._restore_sl(pos)
        elif e == "ACCOUNT_UPDATE":
            for p in msg["a"].get("P", []):
                pos = self.positions.get(p["s"])
                if pos and pos.state in ("open", "closing") and float(p["pa"]) == 0:
                    self._fire(self._finalize_later(pos.symbol))

    async def _after_exit_fill(self, pos: ManagedPosition, o: dict[str, Any]) -> None:
        tp_qty = pos.filled_qty * pos.targets[0][1] if pos.targets else 0
        if pos.tp_hit == 0 and len(pos.targets) > 1 and pos.exit_qty >= tp_qty * 0.999:
            ot = str(o.get("ot", ""))
            if "TAKE_PROFIT" in ot or ot == "LIMIT":
                pos.tp_hit = 1
                self._save()
                if pos.be_after_first and (pos.entry_avg - pos.stop) * pos.direction > 0:
                    async with self._lock(pos.symbol):
                        await self.move_sl(pos, pos.entry_avg, "breakeven")

    async def _restore_sl(self, pos: ManagedPosition) -> None:
        si = self.ctx.meta.symbols[pos.symbol]
        sid = new_client_id("slr")
        try:
            await self.gw.place_algo(
                si,
                pos.exit_side,
                "STOP_MARKET",
                si.round_price(pos.stop, "nearest"),
                sid,
                close_position=True,
            )
            pos.sl_id = sid
            self._save()
            await self.ctx.notify("risk", f"SL for {pos.symbol} re-placed at {pos.stop}")
        except OrderRejectedError as e:
            if e.code == -4130:  # a closePosition stop already exists → still protected
                log.info("sl_restore_not_needed", symbol=pos.symbol)
                return
            await self._emergency_flatten(pos.symbol, f"SL lost and re-place failed ({e.code})")

    # ---- exits -----------------------------------------------------------------------------
    async def close(self, symbol: str, reason: str, ref: float | None = None) -> str:
        pos = self.positions.get(symbol)
        if not pos or pos.state not in ("open",):
            return f"{symbol}: no open position"
        si = self.ctx.meta.symbols[symbol]
        remaining = si.round_qty(pos.filled_qty - pos.exit_qty, market=True)
        bt = self.ctx.book(symbol)
        pos.close_ref = ref or ((bt.bid if pos.direction > 0 else bt.ask) if bt else 0.0)
        pos.close_reason = reason
        pos.state = "closing"
        self._save()
        try:
            await self.gw.place_order(
                si, pos.exit_side, "MARKET", remaining, new_client_id("x"), reduce_only=True
            )
        except OrderRejectedError as e:
            if e.code != -2022:  # -2022: nothing left to reduce
                raise
        return f"{symbol}: closing ({reason})"

    async def close_all(self, reason: str) -> str:
        out = [await self.close(s, reason) for s in list(self.positions)]
        return "; ".join(out) or "no positions"

    async def _emergency_flatten(self, symbol: str, why: str) -> None:
        log.error("flatten", symbol=symbol, why=why)
        pos = self.positions.get(symbol)
        if pos and pos.state == "open":
            pos.state, pos.close_reason = "closing", "forced"  # before cancels → no SL "restore"
        self.ctx.journal.risk_event("flatten", symbol, why=why)
        await self.ctx.notify("risk", f"⚠️ FLATTEN {symbol}: {why}")
        await self.gw.cancel_all(symbol)
        for p in await self.gw.positions():
            if p["symbol"] == symbol:
                amt = Decimal(p["positionAmt"])
                side = "SELL" if amt > 0 else "BUY"
                si = self.ctx.meta.symbols[symbol]
                try:
                    await self.gw.place_order(
                        si, side, "MARKET", abs(amt), new_client_id("fl"), reduce_only=True
                    )
                except OrderRejectedError as e:
                    log.error("flatten_failed", symbol=symbol, code=e.code)
        pos = self.positions.get(symbol)
        if pos:
            if pos.filled_qty > 0:
                pos.state, pos.close_reason = "closing", "forced"
                self._fire(self._finalize_later(symbol))  # never await under the symbol lock
            else:
                self.positions.pop(symbol, None)
                self._save()

    async def kill(self, why: str) -> None:
        """Rule 7: cancel all orders + algo orders, flatten everything, halt."""
        self.ctx.discipline.kill(self.ctx.now_ms())
        self.ctx.persist_discipline()
        self.ctx.journal.risk_event("kill", why=why)
        syms = {p["symbol"] for p in await self.gw.positions()}
        syms |= {o["symbol"] for o in await self.gw.open_orders()}
        syms |= {o["symbol"] for o in await self.gw.open_algo_orders()}
        syms |= set(self.positions)
        for s in syms:
            await self._emergency_flatten(s, f"KILL: {why}")
        await self.ctx.notify(
            "risk",
            f"⛔ KILLED ({why}). Resume only via /resume after "
            f"{self.ctx.cfg.discipline.kill_resume_min_hours} h",
        )

    # ---- finalize --------------------------------------------------------------------------
    async def _finalize_later(self, symbol: str, delay_s: float = 1.5) -> None:
        await asyncio.sleep(delay_s)  # let the last fills land
        async with self._lock(symbol):
            pos = self.positions.get(symbol)
            if pos and pos.state != "closed":
                await self._finalize(pos)

    async def _finalize(self, pos: ManagedPosition) -> None:
        if any(p["symbol"] == pos.symbol for p in await self.gw.positions()):
            return  # not flat yet
        await self.gw.cancel_all(pos.symbol)  # leftover TP / SL
        now = self.ctx.now_ms()
        # one position per symbol ⇒ every fill since the signal belongs to this position
        trades = await self.gw.rest.signed(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": pos.symbol, "startTime": pos.signal_ms, "limit": 1000},
        )
        trades = [t for t in trades if int(t["time"]) >= pos.signal_ms]
        ent = [t for t in trades if t["side"] == pos.side]
        exi = [t for t in trades if t["side"] != pos.side]
        qty_in = sum(float(t["qty"]) for t in ent) or pos.filled_qty
        avg_in = (
            (sum(float(t["price"]) * float(t["qty"]) for t in ent) / qty_in)
            if ent
            else pos.entry_avg
        )
        fees = sum(float(t["commission"]) for t in [*ent, *exi])
        realized = sum(float(t["realizedPnl"]) for t in exi)
        funding = sum(
            float(x["income"])
            for x in await self.gw.income("FUNDING_FEE", pos.entry_ms, now, pos.symbol)
        )
        unit = abs(pos.entry_ref - pos.stop_initial)
        risk_usd = qty_in * unit
        legs = []
        slip_usd = pos.direction * (avg_in - pos.entry_ref) * qty_in
        for t in exi:
            px, q = float(t["price"]), float(t["qty"])
            ref = self._exit_ref(pos, px)
            legs.append(Leg(px, ref, q / qty_in, 0.0, pos.close_reason or "exit"))
            slip_usd += pos.direction * (ref - px) * q
        costs = self.ctx.costs(pos.symbol)
        model_usd = (pos.model_entry_slip_bps / 1e4) * avg_in * qty_in + sum(
            costs.slippage_bps / 1e4 * float(t["price"]) * float(t["qty"]) for t in exi
        )
        net = realized - fees + funding
        exit_reason = pos.close_reason or self._infer_exit(pos, legs)
        res = TradeResult(
            game_id=pos.game_id,
            symbol=pos.symbol,
            side=pos.side,
            tag=pos.tag,
            signal_ms=pos.signal_ms,
            entry_ms=pos.entry_ms,
            exit_ms=now,
            entry_ref=pos.entry_ref,
            entry_px=avg_in,
            stop_initial=pos.stop_initial,
            legs=legs,
            r_net=net / risk_usd if risk_usd else 0.0,
            r_gross=(realized + slip_usd) / risk_usd if risk_usd else 0.0,
            fee_r=fees / risk_usd if risk_usd else 0.0,
            funding_r=-funding / risk_usd if risk_usd else 0.0,
            slippage_r=slip_usd / risk_usd if risk_usd else 0.0,
            mae_r=max(0.0, pos.direction * (avg_in - pos.adverse)) / unit,
            mfe_r=max(0.0, pos.direction * (pos.extreme - avg_in)) / unit,
            exit_reason=exit_reason,
            entry_maker=pos.entry_type == "maker",
            adverse_bps=None,
        )
        self.ctx.journal.trade(
            res,
            pos.status,
            pos.regime,
            model_slippage_r=model_usd / risk_usd if risk_usd else 0.0,
            qty=qty_in,
            pnl_usd=net,
            fees_usd=fees,
            funding_usd=funding,
            risk_frac=pos.f,
            leverage=pos.leverage,
        )
        pos.state = "closed"
        self.positions.pop(pos.symbol, None)
        self._save()
        games = self.ctx.discipline.on_trade_closed(
            pos.symbol, pos.game_id, res.r_net, exit_reason, now
        )
        self.ctx.persist_discipline()
        await self.ctx.notify(
            "close",
            f"CLOSE {pos.symbol} {pos.side} [{pos.game_id}] {exit_reason}: "
            f"R {res.r_net:+.2f} | ${net:+,.2f} (fees ${fees:,.2f}, funding ${funding:+,.2f})",
        )
        if games:
            await self.ctx.notify(
                "risk",
                f"{self.ctx.cfg.discipline.loss_streak_pause_n} "
                f"losses in a row → pause + edge check {games}",
            )
            await self.ctx.edge_check(games)

    def _exit_ref(self, pos: ManagedPosition, px: float) -> float:
        if pos.close_reason and pos.close_ref:
            return pos.close_ref
        levels = [pos.stop, pos.stop_initial] + [pos.target_price(r) for r, _, _ in pos.targets]
        return min(levels, key=lambda lv: abs(lv - px))

    def _infer_exit(self, pos: ManagedPosition, legs: list[Leg]) -> str:
        if not legs:
            return "unknown"
        last = legs[-1].ref
        if last in (pos.stop, pos.stop_initial):
            if pos.tp_hit and pos.stop == pos.entry_avg:
                return "be_stop"
            return "trail_stop" if pos.tp_hit else "stop"
        return "target"

    async def _notify_open(self, pos: ManagedPosition) -> None:
        tps = ", ".join(f"{pos.target_price(r):.6g}" for r, _, _ in pos.targets)
        liq = ""
        for p in await self.gw.positions():
            if p["symbol"] == pos.symbol:
                liq = p.get("liquidationPrice", "")
        await self.ctx.notify(
            "open",
            f"OPEN {pos.symbol} {pos.side} [{pos.game_id} {pos.status}] qty {pos.filled_qty:g} "
            f"@ {pos.entry_avg:.6g} | L {pos.leverage}x | SL {pos.stop:.6g} | TP {tps} | "
            f"liq {liq} | f {pos.f:.3%}",
        )

    # ---- reconciliation --------------------------------------------------------------------
    async def reconcile(self) -> list[str]:
        """REST truth vs our state. Orphan without SL → place SL (known plan) or flatten."""
        notes: list[str] = []
        ex_pos = {p["symbol"]: p for p in await self.gw.positions()}
        algos = await self.gw.open_algo_orders()
        stops: dict[str, list[dict[str, Any]]] = {}
        for a in algos:
            if a.get("orderType") == "STOP_MARKET":
                stops.setdefault(a["symbol"], []).append(a)
        for sym, p in ex_pos.items():
            amt = float(p["positionAmt"])
            pos = self.positions.get(sym)
            if pos is None:
                if not stops.get(sym):
                    notes.append(f"orphan {sym} without SL → flatten")
                    await self._emergency_flatten(sym, "orphan position without SL")
                else:
                    notes.append(f"orphan {sym} has SL (left as is)")
                continue
            if pos.state == "entering":
                pos.state, pos.filled_qty = "open", abs(amt)
                pos.entry_avg = float(p["entryPrice"])
                pos.entry_ms = pos.entry_ms or self.ctx.now_ms()
            if not stops.get(sym):
                notes.append(f"{sym} missing SL → re-place")
                await self._restore_sl(pos)
        for sym, pos in list(self.positions.items()):
            if sym not in ex_pos:
                if pos.state in ("open", "closing"):
                    notes.append(f"{sym} flat on exchange → finalize")
                    await self._finalize(pos)
                elif pos.state == "entering":
                    got = await self.gw.get_order(sym, pos.entry_client_id)
                    if not got or got.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
                        notes.append(f"{sym} stale entry dropped")
                        self.positions.pop(sym, None)
        self._save()
        if notes:
            log.info("reconcile", notes=notes)
        return notes

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(p) for p in self.positions.values()]
