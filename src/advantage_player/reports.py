"""Read-side queries shared by Telegram and the dashboard. Timestamps stored UTC; the
display helpers also render Asia/Bangkok."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from advantage_player.storage.models import (
    EquityRow,
    GameTransitionRow,
    LedgerStatRow,
    SignalRow,
    TradeRow,
)

BKK = ZoneInfo("Asia/Bangkok")
REAL = ("PROBE", "LIVE")


def fmt_both(dt: datetime) -> str:
    dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return f"{dt.astimezone(UTC):%Y-%m-%d %H:%M} UTC / {dt.astimezone(BKK):%H:%M} Bangkok"


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def pnl_window(db: Engine, env: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Real-money PnL with the casino's 'rake' (fees + funding) separated from gross."""
    with Session(db) as s:
        rows = s.scalars(
            select(TradeRow).where(
                TradeRow.env == env,
                TradeRow.stage.in_(REAL),
                TradeRow.exit_ts >= start,
                TradeRow.exit_ts < end,
            )
        ).all()
    net = sum(r.pnl_usd for r in rows)
    fees = sum(r.fees_usd for r in rows)
    funding = sum(r.funding_usd for r in rows)  # signed: + received, − paid
    rake = fees - funding
    by_game = Counter(r.game_id for r in rows)
    return {
        "trades": len(rows),
        "net_usd": net,
        "gross_usd": net + rake,
        "fees_usd": fees,
        "funding_usd": funding,
        "rake_usd": rake,
        "by_game": dict(by_game),
        "r_sum": sum(r.r_net for r in rows),
    }


def no_bet_counts(db: Engine, env: str, start: datetime, end: datetime) -> dict[str, int]:
    with Session(db) as s:
        rows = s.execute(
            select(SignalRow.reason, func.count())
            .where(
                SignalRow.env == env,
                SignalRow.decision == "NO_BET",
                SignalRow.ts >= start,
                SignalRow.ts < end,
            )
            .group_by(SignalRow.reason)
        ).all()
    return {str(r): int(n) for r, n in sorted(rows, key=lambda x: -x[1])}


def transitions(db: Engine, env: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with Session(db) as s:
        rows = s.scalars(
            select(GameTransitionRow)
            .where(
                GameTransitionRow.env == env,
                GameTransitionRow.ts >= start,
                GameTransitionRow.ts < end,
            )
            .order_by(GameTransitionRow.ts)
        ).all()
    return [
        {
            "game": r.game_id,
            "from": r.from_status,
            "to": r.to_status,
            "reason": r.reason,
            "ts": r.ts,
        }
        for r in rows
    ]


def ledger_latest(db: Engine, env: str) -> list[dict[str, Any]]:
    """Latest ledger_stats row per game."""
    with Session(db) as s:
        sub = (
            select(LedgerStatRow.game_id, func.max(LedgerStatRow.id).label("mid"))
            .where(LedgerStatRow.env == env)
            .group_by(LedgerStatRow.game_id)
            .subquery()
        )
        rows = s.scalars(select(LedgerStatRow).join(sub, LedgerStatRow.id == sub.c.mid)).all()
    out = []
    for r in rows:
        st = r.stats
        out.append(
            {
                "game": r.game_id,
                "status": r.status,
                "window": r.window,
                "n": st.get("n"),
                "win_rate": st.get("win_rate"),
                "mean": st.get("mean"),
                "mu_low": st.get("mu_low"),
                "p": st.get("p_value"),
                "delta": st.get("delta_vs_baseline"),
                "delta_low": st.get("delta_low"),
                "bh": r.bh_significant,
                "pass": r.passed,
                "kelly": r.kelly,
                "ts": r.ts,
            }
        )
    return sorted(out, key=lambda x: x["p"] if x["p"] is not None else 1.0)


def shadow_summary(db: Engine, env: str, since: datetime) -> list[dict[str, Any]]:
    """Per game: trade counts / mean R by stage (incl. SHADOW / RESEARCH / BASELINE sims)."""
    with Session(db) as s:
        rows = s.execute(
            select(TradeRow.game_id, TradeRow.stage, func.count(), func.avg(TradeRow.r_net))
            .where(TradeRow.env == env, TradeRow.exit_ts >= since)
            .group_by(TradeRow.game_id, TradeRow.stage)
        ).all()
    agg: dict[str, dict[str, Any]] = defaultdict(dict)
    for g, stage, n, mu in rows:
        agg[g][stage] = {"n": int(n), "mean": float(mu or 0)}
    return [{"game": g, **v} for g, v in sorted(agg.items())]


def equity_series(db: Engine, env: str, since: datetime) -> list[dict[str, Any]]:
    with Session(db) as s:
        rows = s.scalars(
            select(EquityRow)
            .where(EquityRow.env == env, EquityRow.ts >= since)
            .order_by(EquityRow.ts)
        ).all()
    return [
        {
            "ts": r.ts,
            "equity": r.equity,
            "peak": r.peak,
            "drawdown": r.drawdown,
            "open_risk": r.open_risk,
        }
        for r in rows
    ]


def daily_report_text(
    db: Engine, env: str, banner: str, day_start: datetime, equity: float, drawdown: float
) -> str:
    start, end = day_bounds(day_start)
    p = pnl_window(db, env, start, end)
    nb = no_bet_counts(db, env, start, end)
    tr = transitions(db, env, start, end)
    lines = [
        f"📊 [{banner}] Daily report {start:%Y-%m-%d} (UTC day)",
        f"closed at {fmt_both(end)}",
        f"Equity {equity:,.2f} USDT · drawdown {drawdown:.2%}",
        f"Net PnL {p['net_usd']:+,.2f} · gross {p['gross_usd']:+,.2f} · "
        f"rake paid {p['rake_usd']:,.2f} (fees {p['fees_usd']:,.2f}, "
        f"funding {-p['funding_usd']:+,.2f})",
        f"Real trades {p['trades']} (ΣR {p['r_sum']:+.2f})",
    ]
    if p["by_game"]:
        lines.append("Trades per game: " + ", ".join(f"{g} {n}" for g, n in p["by_game"].items()))
    if tr:
        lines.append(
            "Ledger changes: " + "; ".join(f"{t['game']} {t['from']}→{t['to']}" for t in tr[:15])
        )
    if nb:
        lines.append("NO_BET: " + ", ".join(f"{r} {n}" for r, n in list(nb.items())[:12]))
    return "\n".join(lines)
