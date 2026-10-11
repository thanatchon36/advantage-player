"""Dashboard read model. Reads ONLY the database (the bot publishes live state into kv), so the
dashboard works in-process or as a separate service. All times leave here as UTC epoch ms;
the page renders them in Asia/Bangkok."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from advantage_player import reports
from advantage_player.risk import control_queue
from advantage_player.storage.db import ConfigVersion, TableSnapshot
from advantage_player.storage.kv import kv_get
from advantage_player.storage.models import (
    GameRow,
    GameTransitionRow,
    RiskEventRow,
    SignalRow,
    TradeRow,
)

STALE_MS = 20_000


def ms(dt: datetime) -> int:
    """SQLite drops tzinfo; everything is stored UTC."""
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp() * 1000)


def _iso(dt: datetime) -> str:
    return datetime.fromtimestamp(ms(dt) / 1000, UTC).isoformat()


def now() -> datetime:
    return datetime.now(UTC)


def _ms_fields(rows: list[dict[str, Any]], key: str = "ts") -> list[dict[str, Any]]:
    for r in rows:
        if isinstance(r.get(key), datetime):
            r[key] = ms(r[key])
    return rows


def snapshot(db: Engine, env: str) -> dict[str, Any]:
    snap: dict[str, Any] = kv_get(db, env, "live_snapshot") or {}
    start, end = reports.day_bounds(now())
    today = reports.pnl_window(db, env, start, end)
    age = int(now().timestamp() * 1000) - int(snap.get("ts", 0))
    return {
        "env": env,
        "bot_online": bool(snap) and age < STALE_MS,
        "age_ms": age if snap else None,
        "today": today,
        **snap,
    }


def tables(db: Engine) -> dict[str, Any]:
    with Session(db) as s:
        last = s.scalar(select(func.max(TableSnapshot.ts)))
        if last is None:
            return {"ts": None, "rows": []}
        rows = s.scalars(select(TableSnapshot).where(TableSnapshot.ts == last)).all()
    out = []
    for r in rows:
        m = r.metrics
        out.append(
            {
                "symbol": r.symbol,
                "status": r.status,
                "score": r.score,
                "eligible": r.eligible,
                "reasons": r.reasons,
                "cluster": r.cluster,
                "spread_bps": m.get("spread_bps"),
                "depth_usd": min(m.get("depth_bid_usd", 0.0), m.get("depth_ask_usd", 0.0)),
                "quote_volume": m.get("quote_volume"),
                "funding_rate": m.get("funding_rate"),
                "tick_bps": m.get("tick_bps"),
                "atr_bps": m.get("atr_bps", {}),
            }
        )
    return {"ts": ms(last), "rows": sorted(out, key=lambda x: -x["score"])}


def ledger(db: Engine, env: str) -> list[dict[str, Any]]:
    """Every known game with its latest evaluated stats and status history."""
    latest = {r["game"]: r for r in reports.ledger_latest(db, env)}
    hist: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with Session(db) as s:
        games = s.scalars(select(GameRow).where(GameRow.env == env)).all()
        for t in s.scalars(
            select(GameTransitionRow)
            .where(GameTransitionRow.env == env)
            .order_by(GameTransitionRow.ts)
        ):
            hist[t.game_id].append(
                {"ts": ms(t.ts), "from": t.from_status, "to": t.to_status, "reason": t.reason}
            )
    out = []
    for g in games:
        st = latest.get(g.game_id, {})
        out.append(
            {
                "game": g.game_id,
                "status": g.status,
                "since": ms(g.since),
                "window": st.get("window"),
                "n": st.get("n"),
                "win_rate": st.get("win_rate"),
                "mean": st.get("mean"),
                "mu_low": st.get("mu_low"),
                "p": st.get("p"),
                "delta": st.get("delta"),
                "delta_low": st.get("delta_low"),
                "bh": st.get("bh"),
                "pass": st.get("pass"),
                "kelly": st.get("kelly"),
                "history": hist.get(g.game_id, []),
            }
        )
    return sorted(out, key=lambda x: x["p"] if x["p"] is not None else 2.0)


def game(db: Engine, env: str, game_id: str) -> dict[str, Any]:
    """Cumulative-R curve per stage and the R distribution for one game."""
    with Session(db) as s:
        rows = s.scalars(
            select(TradeRow)
            .where(TradeRow.env == env, TradeRow.game_id == game_id)
            .order_by(TradeRow.exit_ts)
        ).all()
    curves: dict[str, list[list[float]]] = defaultdict(list)
    cum: dict[str, float] = defaultdict(float)
    for r in rows:
        cum[r.stage] += r.r_net
        curves[r.stage].append([ms(r.exit_ts), cum[r.stage]])
    return {
        "game": game_id,
        "curves": curves,
        "r": [{"stage": r.stage, "r": r.r_net} for r in rows],
    }


def bankroll(db: Engine, env: str, days: int = 30) -> dict[str, Any]:
    eq = _ms_fields(reports.equity_series(db, env, now() - timedelta(days=days)))
    snap: dict[str, Any] = kv_get(db, env, "live_snapshot") or {}
    return {
        "equity": eq,
        "sweep_baseline": kv_get(db, env, "sweep_baseline", 0.0),
        "risk": snap.get("risk", {}),
        "account": snap.get("account", {}),
    }


def chart(db: Engine, env: str, symbol: str, days: int = 2) -> dict[str, Any]:
    with Session(db) as s:
        rows = s.scalars(
            select(TradeRow)
            .where(
                TradeRow.env == env,
                TradeRow.symbol == symbol,
                TradeRow.stage.in_(reports.REAL),
                TradeRow.exit_ts >= now() - timedelta(days=days),
            )
            .order_by(TradeRow.entry_ts)
        ).all()
    snap: dict[str, Any] = kv_get(db, env, "live_snapshot") or {}
    pos = next((p for p in snap.get("positions", []) if p.get("symbol") == symbol), None)
    return {
        "symbol": symbol,
        "bars": kv_get(db, env, f"bars:{symbol}", []) or [],
        "trades": [
            {
                "side": r.side,
                "entry_ts": ms(r.entry_ts),
                "entry": r.entry_px,
                "exit_ts": ms(r.exit_ts),
                "exit_reason": r.exit_reason,
                "r": r.r_net,
                "game": r.game_id,
            }
            for r in rows
        ],
        "position": pos,
    }


def _trades(db: Engine, env: str, stages: tuple[str, ...], days: int) -> list[TradeRow]:
    with Session(db) as s:
        return list(
            s.scalars(
                select(TradeRow).where(
                    TradeRow.env == env,
                    TradeRow.stage.in_(stages),
                    TradeRow.exit_ts >= now() - timedelta(days=days),
                )
            ).all()
        )


def behavior(db: Engine, env: str, scope: str = "real", days: int = 30) -> dict[str, Any]:
    """Real trades (PROBE/LIVE) by default; scope=shadow shows the simulated SHADOW games."""
    stages = reports.REAL if scope == "real" else ("SHADOW",)
    rows = _trades(db, env, stages, days)
    groups: dict[str, dict[str, dict[str, float]]] = {
        k: defaultdict(lambda: {"n": 0, "r": 0.0, "usd": 0.0})
        for k in ("game", "symbol", "side", "hour_bkk", "regime")
    }
    for r in rows:
        keys = {
            "game": r.game_id,
            "symbol": r.symbol,
            "side": r.side,
            "hour_bkk": f"{datetime.fromtimestamp(ms(r.entry_ts) / 1000, reports.BKK):%H}",
            "regime": r.regime or "?",
        }
        for k, v in keys.items():
            g = groups[k][v]
            g["n"] += 1
            g["r"] += r.r_net
            g["usd"] += r.pnl_usd
    gross = sum(r.r_gross for r in rows)
    fees = sum(r.fee_r for r in rows)
    slip = sum(r.slippage_r for r in rows)
    slip_by_stage = {}
    for st in ("SHADOW", "SIM_PROBE", "SIM_LIVE", "PROBE", "LIVE"):
        sr = _trades(db, env, (st,), days)
        if sr:
            slip_by_stage[st] = {
                "n": len(sr),
                "slippage_r": sum(r.slippage_r for r in sr) / len(sr),
                "model_slippage_r": sum(r.model_slippage_r for r in sr) / len(sr),
            }
    return {
        "scope": scope,
        "n": len(rows),
        "groups": {k: dict(v) for k, v in groups.items()},
        "mae_mfe": [[r.mae_r, r.mfe_r, r.r_net] for r in rows],
        "hold_min": [r.hold_ms / 60_000 for r in rows],
        "gross_r": gross,
        "fee_share": fees / gross if gross > 0 else None,
        "slippage_share": slip / gross if gross > 0 else None,
        "slippage_by_stage": slip_by_stage,
    }


def discipline(db: Engine, env: str, hours: int = 24) -> dict[str, Any]:
    start = now() - timedelta(hours=hours)
    with Session(db) as s:
        events = s.scalars(
            select(RiskEventRow)
            .where(RiskEventRow.env == env)
            .order_by(RiskEventRow.id.desc())
            .limit(200)
        ).all()
        versions = s.scalars(
            select(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(20)
        ).all()
        ev = [
            {"ts": ms(e.ts), "kind": e.kind, "symbol": e.symbol, "detail": e.detail} for e in events
        ]
        cv = [{"ts": ms(v.created_at), "hash": v.hash, "note": v.note} for v in versions]
    return {
        "no_bet": reports.no_bet_counts(db, env, start, now()),
        "hours": hours,
        "risk_events": ev,
        "config_versions": cv,
        "pending": kv_get(db, env, "config_pending", []) or [],
        "overrides": kv_get(db, env, "overrides", {}) or {},
        "controls": _ms_fields(control_queue.recent(db, env)),
    }


EXPORTS = ("trades", "signals", "ledger", "equity", "risk_events")


def export_csv(db: Engine, env: str, kind: str, days: int = 30) -> str:
    since = now() - timedelta(days=days)
    rows: list[dict[str, Any]]
    if kind == "ledger":
        rows = [{k: v for k, v in r.items() if k != "history"} for r in ledger(db, env)]
    elif kind == "equity":
        rows = reports.equity_series(db, env, since)
    else:
        model: Any = {"trades": TradeRow, "signals": SignalRow, "risk_events": RiskEventRow}[kind]
        ts_col = model.exit_ts if kind == "trades" else model.ts
        with Session(db) as s:
            objs = s.scalars(
                select(model).where(model.env == env, ts_col >= since).order_by(model.id)
            ).all()
            cols = [c.name for c in model.__table__.columns]
            rows = [{c: getattr(o, c) for c in cols} for o in objs]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: _iso(v) if isinstance(v, datetime) else v for k, v in r.items()})
    return buf.getvalue()
