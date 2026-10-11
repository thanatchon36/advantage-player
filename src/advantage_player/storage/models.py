"""Persistent records (SPEC §8). All timestamps UTC. `env` separates DEMO and LIVE ledgers.
Exchange ids are BigInteger: Binance ids exceed 32 bits (matters on Postgres)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from advantage_player.storage.db import Base, utcnow

TS = DateTime(timezone=True)


class SignalRow(Base):
    """Every decision, including every decision NOT to bet."""

    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    game_id: Mapped[str] = mapped_column(String(96), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(12))  # game status at decision time
    decision: Mapped[str] = mapped_column(String(16), index=True)  # BET | NO_BET | SHADOW
    reason: Mapped[str] = mapped_column(String(64), index=True, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class GameRow(Base):
    __tablename__ = "games"
    __table_args__ = (UniqueConstraint("env", "game_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    game_id: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(12))
    since: Mapped[datetime] = mapped_column(TS, default=utcnow)
    ref_mu: Mapped[float | None] = mapped_column(Float, nullable=True)  # CUSUM reference
    ref_sigma: Mapped[float | None] = mapped_column(Float, nullable=True)


class GameTransitionRow(Base):
    __tablename__ = "game_transitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    game_id: Mapped[str] = mapped_column(String(96), index=True)
    ts: Mapped[datetime] = mapped_column(TS, default=utcnow)
    from_status: Mapped[str] = mapped_column(String(12))
    to_status: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(String(256))


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    stage: Mapped[str] = mapped_column(String(12), index=True)  # SHADOW|PROBE|LIVE|BASELINE|…
    game_id: Mapped[str] = mapped_column(String(96), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    tag: Mapped[str] = mapped_column(String(64))
    signal_ts: Mapped[datetime] = mapped_column(TS)
    entry_ts: Mapped[datetime] = mapped_column(TS, index=True)
    exit_ts: Mapped[datetime] = mapped_column(TS, index=True)
    entry_ref: Mapped[float] = mapped_column(Float)
    entry_px: Mapped[float] = mapped_column(Float)
    stop_initial: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float, default=0.0)  # 0 for simulated unit trades
    r_net: Mapped[float] = mapped_column(Float)
    r_gross: Mapped[float] = mapped_column(Float)
    fee_r: Mapped[float] = mapped_column(Float)
    funding_r: Mapped[float] = mapped_column(Float)
    slippage_r: Mapped[float] = mapped_column(Float)
    model_slippage_r: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    funding_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mae_r: Mapped[float] = mapped_column(Float)
    mfe_r: Mapped[float] = mapped_column(Float)
    hold_ms: Mapped[int] = mapped_column(BigInteger)
    exit_reason: Mapped[str] = mapped_column(String(16))
    entry_maker: Mapped[bool] = mapped_column(Boolean)
    adverse_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime: Mapped[str] = mapped_column(String(12), default="")
    risk_frac: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[float] = mapped_column(Float, default=0.0)


class LedgerStatRow(Base):
    __tablename__ = "ledger_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    game_id: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(12))
    window: Mapped[str] = mapped_column(String(16))  # stage evaluated (SHADOW / PROBE / LIVE)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON)
    bh_significant: Mapped[bool] = mapped_column(Boolean)
    passed: Mapped[bool] = mapped_column(Boolean)
    kelly: Mapped[float] = mapped_column(Float)


class RiskEventRow(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EquityRow(Base):
    __tablename__ = "equity"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    equity: Mapped[float] = mapped_column(Float)
    peak: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)
    open_risk: Mapped[float] = mapped_column(Float, default=0.0)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    client_id: Mapped[str] = mapped_column(String(40), unique=True)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(24))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    game_id: Mapped[str] = mapped_column(String(96), default="")
    purpose: Mapped[str] = mapped_column(String(16))  # entry | tp | exit | flatten
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AlgoOrderRow(Base):
    __tablename__ = "algo_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    client_algo_id: Mapped[str] = mapped_column(String(40), unique=True)
    algo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(24))
    trigger_price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24))
    purpose: Mapped[str] = mapped_column(String(16))  # sl | tp
    game_id: Mapped[str] = mapped_column(String(96), default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    client_id: Mapped[str] = mapped_column(String(40), index=True)
    trade_id: Mapped[int] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float)
    commission_asset: Mapped[str] = mapped_column(String(12))
    realized_pnl: Mapped[float] = mapped_column(Float)
    maker: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (UniqueConstraint("env", "symbol", "trade_id"),)


class LatencyRow(Base):
    __tablename__ = "latency"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))  # rest_rtt | ws_lag | sl_confirm | …
    ms: Mapped[float] = mapped_column(Float)


class KVRow(Base):
    """Small durable key/value state (halt flags, sweep baseline, peak equity, locks)."""

    __tablename__ = "kv"
    __table_args__ = (UniqueConstraint("env", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8))
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated: Mapped[datetime] = mapped_column(TS, default=utcnow)


class ControlRequest(Base):
    """Dashboard → bot control queue. Only risk-reducing actions (rule 9) are accepted."""

    __tablename__ = "control_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    env: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True, default=utcnow)
    action: Mapped[str] = mapped_column(String(16))
    args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), index=True, default="pending")
    result: Mapped[str] = mapped_column(String(512), default="")
    done_ts: Mapped[datetime | None] = mapped_column(TS, nullable=True)
