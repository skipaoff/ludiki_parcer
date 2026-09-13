"""
VFP: Non-blocking write path to the database — rows are queued in memory, written in batches, and spooled to disk while the database is unavailable.
Changes when: batching, back-pressure or failure handling of database writes changes.
Anti-goal:
1. A caller awaiting the database — submit() only appends; the trading loop never waits for disk or network.
2. Losing rows on an outage or a restart — they go to the spool and are replayed in order before new rows.
3. One bad row blocking everything behind it — rows the database rejects are set aside with the error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Protocol

from app.config.settings import StorageSettings
from app.storage.database import is_connection_error
from app.storage.spool import Row, Spool

log = logging.getLogger(__name__)


class RowStore(Protocol):
    @property
    def ready(self) -> bool: ...

    async def connect(self) -> list[str]: ...

    async def insert_batch(self, rows: list[Row]) -> None: ...

    async def ping(self) -> None: ...

    def mark_down(self, error: BaseException) -> None: ...


Notify = Callable[[str, dict[str, Any]], None]


class _Down(Exception):
    """The store went away in the middle of a flush; the rest waits for the next reconnect."""


class WriteQueue:
    def __init__(
        self,
        store: RowStore,
        spool: Spool,
        settings: StorageSettings,
        notify: Notify = lambda kind, detail: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._spool = spool
        self._settings = settings
        self._notify = notify
        self._clock = clock
        self._pending: deque[Row] = deque()
        self._spooled_rows = spool.row_count()
        self._rejected_rows = 0
        self._up: bool | None = None
        self._next_connect_at = 0.0
        self._next_ping_at = 0.0
        self._last_spool_at = clock()

    @property
    def pending_rows(self) -> int:
        return len(self._pending)

    @property
    def spooled_rows(self) -> int:
        return self._spooled_rows

    @property
    def rejected_rows(self) -> int:
        return self._rejected_rows

    def submit(self, table: str, row: dict[str, Any]) -> None:
        self._pending.append((table, row))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._settings.flush_interval_ms / 1000)
            try:
                await self.flush()
            except Exception:
                log.exception("write queue flush failed")

    async def flush(self, force_connect: bool = False) -> None:
        if not await self._ensure_connected(force_connect):
            self._spool_pending(force=False)
            return
        try:
            if not self._pending and not await self._alive():
                return
            await self._replay_spool()
            while self._pending:
                batch = [self._pending.popleft() for _ in range(min(len(self._pending), self._settings.max_batch_rows))]
                remaining = await self._write(batch)
                if remaining:
                    self._pending.extendleft(reversed(remaining))
                    raise _Down
        except _Down:
            self._spool_pending(force=False)

    async def close(self) -> None:
        """Last attempt to write everything; whatever is left goes to the spool."""
        try:
            await self.flush(force_connect=True)
        finally:
            self._spool_pending(force=True)

    async def _ensure_connected(self, force: bool) -> bool:
        if self._store.ready:
            return True
        now = self._clock()
        if not force and now < self._next_connect_at:
            return False
        self._next_connect_at = now + self._settings.reconnect_interval_s
        try:
            applied = await self._store.connect()
        except Exception as exc:
            self._set_up(False, error=f"{type(exc).__name__}: {exc}")
            return False
        self._set_up(True, migrations_applied=applied, spooled_rows=self._spooled_rows)
        return True

    async def _alive(self) -> bool:
        """While idle, ping now and then so an outage shows up without waiting for the next write."""
        now = self._clock()
        if now < self._next_ping_at:
            return True
        self._next_ping_at = now + self._settings.reconnect_interval_s
        try:
            await self._store.ping()
            return True
        except Exception as exc:
            if not is_connection_error(exc):
                raise
            self._went_down(exc)
            return False

    def _set_up(self, up: bool, **detail: Any) -> None:
        if self._up is up:
            return
        self._up = up
        self._notify("db_connected" if up else "db_unavailable", detail)

    async def _replay_spool(self) -> None:
        for path in self._spool.files():
            rows = self._spool.read(path)
            remaining = await self._write(rows)
            if remaining:
                # Keep the unwritten tail in the same file, so replay order survives the reconnect.
                self._spool.replace(path, remaining)
                self._spooled_rows -= len(rows) - len(remaining)
                raise _Down
            self._spool.remove(path)
            self._spooled_rows -= len(rows)

    async def _write(self, rows: list[Row]) -> list[Row]:
        """Write rows; returns the rows left unwritten because the store went down."""
        try:
            await self._store.insert_batch(rows)
            return []
        except Exception as exc:
            if is_connection_error(exc):
                self._went_down(exc)
                return rows
            log.warning("batch of %d rows rejected (%s), retrying row by row", len(rows), exc)
        for index, row in enumerate(rows):
            try:
                await self._store.insert_batch([row])
            except Exception as exc:
                if is_connection_error(exc):
                    self._went_down(exc)
                    return rows[index:]
                # Logged and counted, not journaled: a broken events table would reject its own report forever.
                log.error("row for %s rejected by the database: %s", row[0], exc)
                self._spool.reject([row], f"{type(exc).__name__}: {exc}")
                self._rejected_rows += 1
        return []

    def _went_down(self, exc: BaseException) -> None:
        self._store.mark_down(exc)
        self._next_connect_at = self._clock() + self._settings.reconnect_interval_s
        self._set_up(False, error=f"{type(exc).__name__}: {exc}")

    def _spool_pending(self, force: bool) -> None:
        """While the store is down, move memory to disk in chunks rather than one file per flush."""
        if not self._pending:
            return
        now = self._clock()
        due = now - self._last_spool_at >= self._settings.reconnect_interval_s
        if not (force or due or len(self._pending) >= self._settings.max_batch_rows):
            return
        rows = list(self._pending)
        self._spool.append(rows)
        self._pending.clear()
        self._spooled_rows += len(rows)
        self._last_spool_at = now
