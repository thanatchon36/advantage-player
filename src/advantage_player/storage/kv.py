"""Durable small state per env (halt flags, peaks, locks)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from advantage_player.storage.db import utcnow
from advantage_player.storage.models import KVRow


def kv_get(engine: Engine, env: str, key: str, default: Any = None) -> Any:
    with Session(engine) as s:
        row = s.scalars(select(KVRow).where(KVRow.env == env, KVRow.key == key)).first()
        return row.value.get("v", default) if row else default


def kv_set(engine: Engine, env: str, key: str, value: Any) -> None:
    with Session(engine) as s:
        row = s.scalars(select(KVRow).where(KVRow.env == env, KVRow.key == key)).first()
        if row is None:
            s.add(KVRow(env=env, key=key, value={"v": value}))
        else:
            row.value = {"v": value}
            row.updated = utcnow()
        s.commit()
