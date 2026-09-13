"""
VFP: Settings changed from the interface — their latest values and the full history of every change in `settings_history`.
Changes when: the way runtime settings are persisted changes.
Anti-goal:
1. Secrets in settings — keys stay in the keystore.
2. Overwriting history — every change is a new row with the old and the new value.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import orjson


async def load_latest(pool: asyncpg.Pool, prefix: str) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (key) key, new_value::text AS value
        FROM settings_history
        WHERE key LIKE $1
        ORDER BY key, ts DESC, id DESC
        """,
        f"{prefix}%",
    )
    return {row["key"]: orjson.loads(row["value"]) for row in rows if row["value"] is not None}


async def record_change(pool: asyncpg.Pool, key: str, old_value: Any, new_value: Any) -> None:
    await pool.execute(
        "INSERT INTO settings_history (key, old_value, new_value) VALUES ($1, $2::text::jsonb, $3::text::jsonb)",
        key,
        orjson.dumps(old_value, default=str).decode(),
        orjson.dumps(new_value, default=str).decode(),
    )
