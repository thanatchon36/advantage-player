"""Dashboard → bot control queue (rule 9: risk-reducing only).

The dashboard only writes rows; the bot polls them and runs each through `Controls`, which
refuses anything that would raise risk. Unknown actions are rejected at enqueue time, and
requests the bot didn't pick up within `max_age_s` expire instead of running late.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from advantage_player.risk.controls import Controls
from advantage_player.storage.db import utcnow
from advantage_player.storage.models import ControlRequest

ACTIONS = ("pause", "close", "kill", "risk_scale", "lower")


def enqueue(db: Engine, env: str, action: str, args: dict[str, Any]) -> int:
    if action not in ACTIONS:
        raise ValueError(f"unknown control action {action!r}")
    with Session(db) as s:
        row = ControlRequest(env=env, action=action, args=args)
        s.add(row)
        s.commit()
        return row.id


def recent(db: Engine, env: str, limit: int = 20) -> list[dict[str, Any]]:
    with Session(db) as s:
        rows = s.scalars(
            select(ControlRequest)
            .where(ControlRequest.env == env)
            .order_by(ControlRequest.id.desc())
            .limit(limit)
        ).all()
    return [
        {
            "id": r.id,
            "ts": r.ts,
            "action": r.action,
            "args": r.args,
            "status": r.status,
            "result": r.result,
        }
        for r in rows
    ]


async def _run(c: Controls, action: str, args: dict[str, Any]) -> str:
    if action == "pause":
        return await c.pause()
    if action == "close":
        sym = str(args.get("symbol") or "all")
        return await c.close(None if sym.lower() == "all" else sym.upper())
    if action == "kill":
        return await c.kill()
    if action == "risk_scale":
        return await c.set_risk_scale(float(args["value"]))
    if action == "lower":
        return await c.lower(str(args["path"]), args["value"])
    return f"refused: unknown action {action}"


async def process_pending(
    db: Engine, env: str, controls: Controls, max_age_s: float = 60.0
) -> list[str]:
    now = utcnow()
    with Session(db) as s:
        rows = s.scalars(
            select(ControlRequest)
            .where(ControlRequest.env == env, ControlRequest.status == "pending")
            .order_by(ControlRequest.id)
        ).all()
        out = []
        for r in rows:
            ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=now.tzinfo)
            if now - ts > timedelta(seconds=max_age_s):
                r.status, r.result = "expired", "not picked up in time"
            else:
                try:
                    r.result = (await _run(controls, r.action, r.args))[:512]
                    r.status = "refused" if r.result.startswith("refused") else "done"
                except Exception as e:
                    r.status, r.result = "error", repr(e)[:512]
            r.done_ts = now
            out.append(f"{r.action}: {r.result}")
            s.commit()
    return out
