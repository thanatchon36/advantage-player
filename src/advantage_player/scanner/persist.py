"""Persist scan results to table_snapshots."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from advantage_player.scanner.scanner import ScanResult
from advantage_player.storage.db import TableSnapshot


def save_scan(engine: Engine, res: ScanResult) -> int:
    ts = datetime.fromtimestamp(res.ts_ms / 1000, UTC)
    rows = []
    for sym, m in res.metrics.items():
        status = "open" if sym in res.open else "watch" if sym in res.watch else "none"
        rows.append(
            TableSnapshot(
                ts=ts,
                symbol=sym,
                status=status,
                score=res.scores[sym],
                eligible=not res.reasons[sym],
                reasons=res.reasons[sym],
                cluster=res.cluster_of(sym),
                metrics=asdict(m),
            )
        )
    with Session(engine) as s:
        s.add_all(rows)
        s.commit()
    return len(rows)
