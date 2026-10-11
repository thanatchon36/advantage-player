"""Bankroll = futures wallet equity (totalMarginBalance, USDT). Tracks peak, start-of-day,
drawdown steps, the 10 % hard-kill trigger, and the "take chips off the table" sweep.

Transfers out of the futures wallet lower peak / start-of-day / sweep baseline by the same
amount so moving profit to spot never looks like a drawdown.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from advantage_player.config.settings import RiskCfg
from advantage_player.risk.sizing import HALT, dd_multiplier
from advantage_player.storage.kv import kv_get, kv_set
from advantage_player.storage.models import EquityRow

log = structlog.get_logger(__name__)
Alert = Callable[[str, str], Awaitable[None]]


@dataclass
class EquitySnapshot:
    equity: float
    peak: float
    sod: float
    drawdown: float
    daily_loss: float
    dd_step: float


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


class AccountTracker:
    def __init__(self, db: Engine, env: str, cfg: RiskCfg, alert: Alert) -> None:
        self.db = db
        self.env = env
        self.cfg = cfg
        self.alert = alert
        self.equity = 0.0
        self.peak = float(kv_get(db, env, "equity_peak", 0.0))
        self.sweep_baseline = float(kv_get(db, env, "sweep_baseline", 0.0))
        self._sod: dict[str, float] = dict(kv_get(db, env, "sod", {}) or {})
        self._last_step = 1.0
        self._last_row_ms = 0
        self._last_sweep_note_day = ""
        self._reset_baseline = False
        self.on_halt: Callable[[str], Awaitable[None]] | None = None

    def snapshot(self, now_ms: int | None = None) -> EquitySnapshot:
        now = now_ms or int(time.time() * 1000)
        sod = self._sod.get(utc_day(now), self.equity)
        dd = 1 - self.equity / self.peak if self.peak > 0 else 0.0
        dl = max(0.0, 1 - self.equity / sod) if sod > 0 else 0.0
        return EquitySnapshot(
            self.equity, self.peak, sod, max(dd, 0.0), dl, dd_multiplier(max(dd, 0.0), self.cfg)
        )

    async def update(
        self, equity: float, now_ms: int | None = None, open_risk: float = 0.0
    ) -> None:
        now = now_ms or int(time.time() * 1000)
        self.equity = equity
        if equity > self.peak:
            self.peak = equity
            kv_set(self.db, self.env, "equity_peak", self.peak)
        day = utc_day(now)
        if day not in self._sod:
            self._sod = {day: equity}  # keep only today
            kv_set(self.db, self.env, "sod", self._sod)
        if self.sweep_baseline <= 0 or self._reset_baseline:
            self._reset_baseline = False
            self.sweep_baseline = equity
            kv_set(self.db, self.env, "sweep_baseline", equity)
        snap = self.snapshot(now)
        if snap.dd_step != self._last_step:
            if snap.dd_step == HALT:
                await self.alert(
                    "risk",
                    f"⛔ drawdown {snap.drawdown:.1%} ≥ {self.cfg.dd_halt_at:.0%} → HARD KILL",
                )
                if self.on_halt:
                    await self.on_halt(f"drawdown {snap.drawdown:.1%}")
            else:
                await self.alert(
                    "risk", f"drawdown {snap.drawdown:.1%} → size multiplier {snap.dd_step:g}"
                )
            self._last_step = snap.dd_step
        if (
            self.sweep_baseline > 0
            and equity > self.sweep_baseline * self.cfg.sweep_trigger
            and self._last_sweep_note_day != day
        ):
            excess = equity - self.sweep_baseline
            await self.alert(
                "sweep",
                f"💰 equity {equity:,.2f} > {self.cfg.sweep_trigger:g}× "
                f"baseline {self.sweep_baseline:,.2f}: consider moving "
                f"{excess:,.2f} USDT to spot",
            )
            self._last_sweep_note_day = day
        if now - self._last_row_ms >= 60_000:
            self._last_row_ms = now
            with Session(self.db) as s:
                s.add(
                    EquityRow(
                        env=self.env,
                        equity=equity,
                        peak=self.peak,
                        drawdown=snap.drawdown,
                        open_risk=open_risk,
                    )
                )
                s.commit()

    def apply_transfers(self, rows: list[dict[str, object]]) -> int:
        """Apply /fapi/v1/income TRANSFER rows once each (keyed by tranId)."""
        done = set(kv_get(self.db, self.env, "transfer_ids", []) or [])
        n = 0
        for r in sorted(rows, key=lambda x: int(str(x.get("time", 0)))):
            tid = str(r.get("tranId"))
            if tid in done:
                continue
            done.add(tid)
            self.on_transfer(float(str(r["income"])), int(str(r.get("time", 0))) or None)
            n += 1
        if n:
            kv_set(self.db, self.env, "transfer_ids", sorted(done)[-500:])
        return n

    def on_transfer(self, amount: float, now_ms: int | None = None) -> None:
        """Signed transfer into (+) / out of (−) the futures wallet."""
        if amount >= 0:
            return
        now = now_ms or int(time.time() * 1000)
        self.peak = max(self.peak + amount, 0.0)
        kv_set(self.db, self.env, "equity_peak", self.peak)
        day = utc_day(now)
        if day in self._sod:
            self._sod[day] = max(self._sod[day] + amount, 0.0)
            kv_set(self.db, self.env, "sod", self._sod)
        # a transfer out is a sweep → reset the baseline at the next equity update
        self._reset_baseline = True
