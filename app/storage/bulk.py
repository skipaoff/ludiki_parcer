"""
VFP: Writes many rows in one SQL statement and proves the database stored every one of them.
Changes when: bulk write volume or the verification rule changes.
Anti-goal:
1. asyncpg executemany — on the Windows selector loop it once dropped the tail of a batch without raising (13.09.2026).
2. Trusting a write without a count — the statement's row count must equal the number of rows sent.
3. Table or column names from outside the code — identifiers are checked, values travel as one JSON parameter.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import orjson

_IDENTIFIER_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


class BulkWriteError(RuntimeError):
    pass


def identifier(name: str) -> str:
    if not name or name[0].isdigit() or not set(name) <= _IDENTIFIER_OK:
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"cannot store {type(value).__name__} in a bulk write")


def rows_json(rows: list[dict[str, Any]]) -> str:
    return orjson.dumps(rows, default=_json_default).decode()


def insert_select_sql(table: str, columns: tuple[str, ...], extra: dict[str, str] | None = None, tail: str = "") -> str:
    """
    INSERT ... SELECT from jsonb_populate_recordset, so PostgreSQL casts every value to the column type.
    extra maps additional target columns to SQL expressions (e.g. {"updated_at": "now()"}); tail is appended (ON CONFLICT).
    """
    extra = extra or {}
    target = ", ".join(identifier(column) for column in (*columns, *extra))
    source = ", ".join([*(identifier(column) for column in columns), *extra.values()])
    table_sql = identifier(table)
    return (
        f"INSERT INTO {table_sql} ({target}) "
        f"SELECT {source} FROM jsonb_populate_recordset(NULL::{table_sql}, $1::text::jsonb) {tail}"
    ).strip()


def affected_rows(status: str) -> int:
    """Row count from a command tag such as 'INSERT 0 42'."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except ValueError as exc:
        raise BulkWriteError(f"unexpected command status {status!r}") from exc


async def bulk_insert(
    connection: Any,
    table: str,
    rows: list[dict[str, Any]],
    extra: dict[str, str] | None = None,
    tail: str = "",
) -> int:
    """Insert rows that share one column set; raises BulkWriteError unless every row was stored."""
    if not rows:
        return 0
    columns = tuple(rows[0])
    if any(tuple(row) != columns for row in rows):
        raise ValueError("all rows of one bulk insert must have the same columns in the same order")
    status = await connection.execute(insert_select_sql(table, columns, extra, tail), rows_json(rows))
    stored = affected_rows(status)
    if stored != len(rows):
        raise BulkWriteError(f"{table}: sent {len(rows)} rows, database stored {stored}")
    return stored
