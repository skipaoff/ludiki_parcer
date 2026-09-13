"""
VFP: A ready-to-use PostgreSQL connection pool — the local cluster started if needed and the schema migrated.
Changes when: how the terminal finds, starts or connects to its database changes.
Anti-goal:
1. The terminal refusing to start because the database is down — callers keep working and retry later.
2. Stopping the cluster on exit — other tools and the next start reuse it.
3. The password in a DSN string, a file or a log line — it goes straight from the keystore to the driver.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

import asyncpg
import orjson

from app.config.settings import LOOPBACK_HOSTS, DatabaseSettings
from app.storage.bulk import bulk_insert, identifier
from app.storage.migrations import migrate

log = logging.getLogger(__name__)

def is_connection_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            OSError,
            TimeoutError,
            asyncpg.InterfaceError,
            asyncpg.PostgresConnectionError,
            asyncpg.exceptions.OperatorInterventionError,
        ),
    )


# Tables whose rows are written more than once during their life (created, then completed).
UPSERT_KEYS: dict[str, tuple[str, ...]] = {
    "opportunity_episodes": ("id",),
    "trades": ("id",),
    "orders": ("id",),
}


def upsert_tail(keys: tuple[str, ...], columns: tuple[str, ...]) -> str:
    updates = ", ".join(f"{identifier(column)} = EXCLUDED.{identifier(column)}" for column in columns if column not in keys)
    target = ", ".join(identifier(key) for key in keys)
    return f"ON CONFLICT ({target}) DO UPDATE SET {updates}" if updates else f"ON CONFLICT ({target}) DO NOTHING"


def _json_dumps(value: Any) -> str:
    return orjson.dumps(value, default=str).decode()


async def _init_connection(connection: asyncpg.Connection) -> None:
    for kind in ("json", "jsonb"):
        await connection.set_type_codec(kind, encoder=_json_dumps, decoder=orjson.loads, schema="pg_catalog")


class Database:
    def __init__(
        self,
        settings: DatabaseSettings,
        password: Callable[[], str | None],
        migrations_dir: Path,
        logs_dir: Path,
    ) -> None:
        self._settings = settings
        self._password = password
        self._migrations_dir = migrations_dir
        self._logs_dir = logs_dir
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise ConnectionError("database is not connected")
        return self._pool

    async def connect(self) -> list[str]:
        """Start the cluster if allowed, open the pool, apply migrations. Returns migrations applied now."""
        async with self._lock:
            if self._pool is not None:
                return []
            pool: asyncpg.Pool | None = None
            try:
                await self._ensure_cluster()
                pool = await asyncpg.create_pool(
                    host=self._settings.host,
                    port=self._settings.port,
                    user=self._settings.user,
                    password=self._password(),
                    database=self._settings.name,
                    min_size=1,
                    max_size=4,
                    timeout=self._settings.connect_timeout_s,
                    init=_init_connection,
                )
                async with pool.acquire() as connection:
                    applied = await migrate(connection, self._migrations_dir)
            except Exception as exc:
                if pool is not None:
                    pool.terminate()
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise
            self._pool = pool
            self.last_error = None
            return applied

    def mark_down(self, error: BaseException) -> None:
        """Forget the pool after a connection failure; the next connect() builds a fresh one."""
        self.last_error = f"{type(error).__name__}: {error}"
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.terminate()

    async def ping(self) -> None:
        """Cheap liveness check; raises a connection error when the server is gone."""
        await self.pool.fetchval("SELECT 1", timeout=self._settings.connect_timeout_s)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def insert_batch(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        """
        Insert rows of any tables in one transaction, one verified statement per table and column set, in the order
        each group first appeared (a parent row submitted before its children is stored before them).
        Tables in UPSERT_KEYS are upserted; several versions of one row in a batch collapse to the newest.
        """
        groups: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
        for table, row in rows:
            groups.setdefault((table, tuple(row)), []).append(row)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for (table, columns), group in groups.items():
                    keys = UPSERT_KEYS.get(table)
                    if keys is None:
                        await bulk_insert(connection, table, group)
                        continue
                    latest = {tuple(row[key] for key in keys): row for row in group}
                    await bulk_insert(connection, table, list(latest.values()), tail=upsert_tail(keys, columns))

    async def _ensure_cluster(self) -> None:
        if not self._settings.autostart or self._settings.host not in LOOPBACK_HOSTS:
            return
        pg_ctl = self._settings.pg_bin_dir / ("pg_ctl.exe" if (self._settings.pg_bin_dir / "pg_ctl.exe").exists() else "pg_ctl")
        if not pg_ctl.exists():
            log.warning("pg_ctl not found in %s, not starting the cluster", self._settings.pg_bin_dir)
            return
        data_dir = str(self._settings.pg_data_dir)
        status = await asyncio.to_thread(_run, [str(pg_ctl), "status", "-D", data_dir])
        if status == 0:
            return
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        log.info("starting PostgreSQL cluster in %s", data_dir)
        code = await asyncio.to_thread(
            _run,
            [str(pg_ctl), "start", "-D", data_dir, "-l", str(self._logs_dir / "postgres.log"), "-w", "-t", "60"],
        )
        if code != 0:
            raise ConnectionError(f"pg_ctl start exited with {code}, see {self._logs_dir / 'postgres.log'}")


def _run(command: list[str]) -> int:
    # No pipes: the postmaster inherits handles, and a pipe it holds would never close.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        check=False,
    ).returncode
