"""Writes signals (incl. NO_BET + reason), trades and risk events to the database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from advantage_player.sim.fills import TradeResult
from advantage_player.storage.models import RiskEventRow, SignalRow, TradeRow


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


class Journal:
    def __init__(self, db: Engine, env: str) -> None:
        self.db = db
        self.env = env

    def signal(
        self,
        ts_ms: int,
        game_id: str,
        symbol: str,
        side: str,
        status: str,
        decision: str,
        reason: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        with Session(self.db) as s:
            s.add(
                SignalRow(
                    env=self.env,
                    ts=ms_to_dt(ts_ms),
                    game_id=game_id,
                    symbol=symbol,
                    side=side,
                    status=status,
                    decision=decision,
                    reason=reason,
                    detail=detail or {},
                )
            )
            s.commit()

    def trade(
        self,
        r: TradeResult,
        stage: str,
        regime: str = "",
        *,
        model_slippage_r: float | None = None,
        qty: float = 0.0,
        pnl_usd: float = 0.0,
        fees_usd: float = 0.0,
        funding_usd: float = 0.0,
        risk_frac: float = 0.0,
        leverage: float = 0.0,
    ) -> TradeRow:
        row = TradeRow(
            env=self.env,
            stage=stage,
            game_id=r.game_id,
            symbol=r.symbol,
            side=r.side,
            tag=r.tag,
            signal_ts=ms_to_dt(r.signal_ms),
            entry_ts=ms_to_dt(r.entry_ms),
            exit_ts=ms_to_dt(r.exit_ms),
            entry_ref=r.entry_ref,
            entry_px=r.entry_px,
            stop_initial=r.stop_initial,
            qty=qty,
            r_net=r.r_net,
            r_gross=r.r_gross,
            fee_r=r.fee_r,
            funding_r=r.funding_r,
            slippage_r=r.slippage_r,
            model_slippage_r=r.slippage_r if model_slippage_r is None else model_slippage_r,
            pnl_usd=pnl_usd,
            fees_usd=fees_usd,
            funding_usd=funding_usd,
            mae_r=r.mae_r,
            mfe_r=r.mfe_r,
            hold_ms=r.hold_ms,
            exit_reason=r.exit_reason,
            entry_maker=r.entry_maker,
            adverse_bps=r.adverse_bps,
            regime=regime,
            risk_frac=risk_frac,
            leverage=leverage,
        )
        with Session(self.db, expire_on_commit=False) as s:
            s.add(row)
            s.commit()
        return row

    def risk_event(self, kind: str, symbol: str = "", **detail: Any) -> None:
        with Session(self.db) as s:
            s.add(RiskEventRow(env=self.env, kind=kind, symbol=symbol, detail=detail))
            s.commit()
