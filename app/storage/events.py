"""
VFP: Reads of the `events` journal table for the interface.
Changes when: the interface needs the journal filtered or paged differently.
Anti-goal:
1. Writes here — journal rows reach the table only through the write queue.
"""

from __future__ import annotations

from typing import Any

import asyncpg


async def recent_events(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    """Newest last, in the same shape as JournalEvent.to_wire()."""
    rows = await pool.fetch(
        "SELECT ts, level, source, type, exchange, trade_id, payload FROM events ORDER BY ts DESC, id DESC LIMIT $1",
        limit,
    )
    return [
        {
            "ts_ms": int(row["ts"].timestamp() * 1000),
            "level": str(row["level"]),
            "source": row["source"],
            "type": row["type"],
            "exchange": row["exchange"],
            "trade_id": row["trade_id"],
            "payload": row["payload"] or {},
        }
        for row in reversed(rows)
    ]
