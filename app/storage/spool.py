"""
VFP: Durable on-disk buffer of rows that could not be written to the database yet, replayed in order once it is back.
Changes when: the spool format or rotation changes.
Anti-goal:
1. Losing rows when the database is down or the process restarts — every batch hits the disk before it leaves memory.
2. Blocking replay forever on a row the database will never accept — such rows move to a rejected file.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson

Row = tuple[str, dict[str, Any]]

_DECIMAL = "__decimal__"
_DATETIME = "__datetime__"


def encode_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {_DECIMAL: str(value)}
    if isinstance(value, datetime):
        return {_DATETIME: value.isoformat()}
    return value


def decode_value(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        if _DECIMAL in value:
            return Decimal(value[_DECIMAL])
        if _DATETIME in value:
            return datetime.fromisoformat(value[_DATETIME])
    return value


def encode_row(table: str, row: dict[str, Any]) -> bytes:
    payload = {"table": table, "row": {key: encode_value(value) for key, value in row.items()}}
    return orjson.dumps(payload, default=str) + b"\n"


def decode_row(line: bytes) -> Row:
    payload = orjson.loads(line)
    return payload["table"], {key: decode_value(value) for key, value in payload["row"].items()}


class Spool:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def append(self, rows: list[Row]) -> Path:
        """Write one batch as its own file, flushed to disk before returning."""
        path = self._directory / f"spool-{time.time_ns()}.jsonl"
        self._write(path, rows)
        return path

    def replace(self, path: Path, rows: list[Row]) -> None:
        """Atomically swap a spool file's contents for rows, keeping its place in the replay order."""
        temporary = path.with_suffix(".tmp")
        self._write(temporary, rows)
        os.replace(temporary, path)

    @staticmethod
    def _write(path: Path, rows: list[Row]) -> None:
        with path.open("wb") as handle:
            for table, row in rows:
                handle.write(encode_row(table, row))
            handle.flush()
            os.fsync(handle.fileno())

    def files(self) -> list[Path]:
        return sorted(self._directory.glob("spool-*.jsonl"))

    @staticmethod
    def read(path: Path) -> list[Row]:
        with path.open("rb") as handle:
            return [decode_row(line) for line in handle if line.strip()]

    @staticmethod
    def remove(path: Path) -> None:
        path.unlink(missing_ok=True)

    def reject(self, rows: list[Row], error: str) -> None:
        path = self._directory / f"rejected-{time.strftime('%Y%m%d')}.jsonl"
        with path.open("ab") as handle:
            for table, row in rows:
                line = orjson.loads(encode_row(table, row))
                line["error"] = error
                handle.write(orjson.dumps(line) + b"\n")

    def row_count(self) -> int:
        total = 0
        for path in self.files():
            with path.open("rb") as handle:
                total += sum(1 for line in handle if line.strip())
        return total
