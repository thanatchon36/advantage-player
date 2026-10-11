"""Telegram command handlers. Read-only views + risk-REDUCING controls only (rule 9)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from advantage_player import reports
from advantage_player.notify.telegram import Handler

if TYPE_CHECKING:
    from advantage_player.app import Bot


def build_commands(bot: Bot) -> dict[str, Handler]:
    c = bot.controls

    async def status(_: list[str]) -> str:
        st = bot.status()
        return "\n".join(f"{k}: {v}" for k, v in st.items())

    async def pnl(_: list[str]) -> str:
        start, end = reports.day_bounds(datetime.now(UTC))
        p = reports.pnl_window(bot.db, bot.env, start, end)
        snap = bot.account.snapshot()
        return (
            f"[{bot.banner}] today (UTC): net {p['net_usd']:+,.2f} · gross "
            f"{p['gross_usd']:+,.2f} · rake {p['rake_usd']:,.2f} · trades {p['trades']}\n"
            f"equity {snap.equity:,.2f} · peak {snap.peak:,.2f} · dd {snap.drawdown:.2%} · "
            f"daily loss {snap.daily_loss:.2%} · size ×{snap.dd_step:g}"
        )

    async def ledger(_: list[str]) -> str:
        rows = reports.ledger_latest(bot.db, bot.env)[:15]
        if not rows:
            counts = bot.status()["games"]
            return f"no evaluated games yet; game statuses: {counts}"
        return "\n".join(
            f"{r['game']} {r['status']} n={r['n']} μ={_f(r['mean'])} μlow={_f(r['mu_low'])} "
            f"p={_f(r['p'])} BH={'Y' if r['bh'] else '-'} PASS={'Y' if r['pass'] else '-'}"
            for r in rows
        )

    async def tables(_: list[str]) -> str:
        sc = bot.scan
        if not sc:
            return "no scan yet"
        lines = [f"open: {sc.open or '—'}", f"watch: {sc.watch}"]
        for s in [*sc.open, *sc.watch][:12]:
            m = sc.metrics[s]
            lines.append(
                f"{s} score {sc.scores[s]:.2f} spread {m.spread_bps:.1f}bp "
                f"fund {m.funding_rate:+.4%} cl {sc.cluster_of(s)} "
                f"{','.join(sc.reasons[s]) or 'eligible'}"
            )
        return "\n".join(lines)

    async def pause(_: list[str]) -> str:
        return await c.pause()

    async def resume(_: list[str]) -> str:
        return await c.resume()

    async def close(args: list[str]) -> str:
        if not args:
            return "usage: /close <SYMBOL|all>"
        return await c.close(None if args[0].lower() == "all" else args[0].upper())

    async def kill(_: list[str]) -> str:
        return await c.kill()

    async def risk(args: list[str]) -> str:
        if not args:
            v = c.view()
            return (
                "risk (lower-only): "
                + ", ".join(f"{k}={v[k]}" for k in v)
                + "\nset: /risk scale <0..current> | /risk set <path> <lower value>"
            )
        if args[0] == "scale" and len(args) == 2:
            return await c.set_risk_scale(float(args[1]))
        if args[0] == "set" and len(args) == 3:
            return await c.lower(args[1], args[2])
        return "usage: /risk | /risk scale 0.5 | /risk set risk.max_risk_per_trade 0.005"

    return {
        "status": status,
        "pnl": pnl,
        "ledger": ledger,
        "tables": tables,
        "pause": pause,
        "resume": resume,
        "close": close,
        "kill": kill,
        "risk": risk,
    }


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.3f}"
