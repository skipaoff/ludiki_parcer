"""
VFP: Brings the database schema up to the numbered SQL files in db/, each applied exactly once and never edited afterwards.
Changes when: the migration file layout or the bookkeeping table changes.
Anti-goal:
1. Silently re-applying or skipping a file whose contents changed after it was applied — that is drift and stops startup.
2. An ORM or generated migrations — the SQL files are the single source of truth, CI applies the same files with psql.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.storage.sql_script import split_statements

FILE_PATTERN = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
LOCK_ID = 0x1D1C  # advisory lock so two terminal processes never migrate at once

BOOKKEEPING_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    filename    TEXT        NOT NULL,
    sha256      TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationDrift(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationFile:
    version: str
    filename: str
    sha256: str
    sql: str


def checksum(sql: str) -> str:
    """Line-ending independent, so a CRLF checkout on Windows matches an LF checkout in CI."""
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(directory: Path) -> list[MigrationFile]:
    files = []
    for path in sorted(directory.iterdir()):
        match = FILE_PATTERN.match(path.name)
        if not match:
            continue
        sql = path.read_text(encoding="utf-8")
        files.append(MigrationFile(match.group(1), path.name, checksum(sql), sql))
    versions = [file.version for file in files]
    if len(versions) != len(set(versions)):
        raise MigrationDrift(f"duplicate migration versions in {directory}")
    return files


def pending(files: list[MigrationFile], applied: dict[str, str]) -> list[MigrationFile]:
    """Files still to apply, in order. applied maps version to the checksum recorded when it ran."""
    known = {file.version: file for file in files}
    for version, recorded in applied.items():
        file = known.get(version)
        if file is None:
            raise MigrationDrift(f"migration {version} is applied in the database but missing in db/")
        if file.sha256 != recorded:
            raise MigrationDrift(f"{file.filename} changed after it was applied; add a new numbered file instead")
    todo = [file for file in files if file.version not in applied]
    if todo and applied and todo[0].version < max(applied):
        raise MigrationDrift(f"{todo[0].filename} is older than the latest applied migration")
    return todo


async def migrate(connection, directory: Path) -> list[str]:
    """Apply pending migrations on an asyncpg connection. Returns applied filenames."""
    files = discover(directory)
    await connection.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
    try:
        await connection.execute(BOOKKEEPING_SQL)
        rows = await connection.fetch("SELECT version, sha256 FROM schema_migrations")
        todo = pending(files, {row["version"]: row["sha256"] for row in rows})
        for file in todo:
            # Statement by statement, like psql: files manage their own transactions where they need one.
            for statement in split_statements(file.sql):
                await connection.execute(statement)
            await connection.execute(
                "INSERT INTO schema_migrations (version, filename, sha256) VALUES ($1, $2, $3)",
                file.version,
                file.filename,
                file.sha256,
            )
        return [file.filename for file in todo]
    finally:
        # A failed statement inside a file's own BEGIN leaves the session aborted, and a broken connection
        # drops the advisory lock by itself — neither may hide the original error.
        with contextlib.suppress(Exception):
            if connection.is_in_transaction():
                await connection.execute("ROLLBACK")
            await connection.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
