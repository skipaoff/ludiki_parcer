import os
from pathlib import Path

import pytest

from app.config.settings import REPO_ROOT
from app.storage.migrations import MigrationDrift, checksum, discover, migrate, pending


def write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def test_discover_orders_by_version_and_ignores_other_files(tmp_path: Path):
    write(tmp_path, "002_second.sql", "SELECT 2;")
    write(tmp_path, "001_first.sql", "SELECT 1;")
    write(tmp_path, "notes.md", "not a migration")

    files = discover(tmp_path)

    assert [file.filename for file in files] == ["001_first.sql", "002_second.sql"]


def test_checksum_ignores_line_endings():
    assert checksum("SELECT 1;\r\nSELECT 2;\r\n") == checksum("SELECT 1;\nSELECT 2;\n")


def test_pending_skips_applied_and_detects_drift(tmp_path: Path):
    write(tmp_path, "001_first.sql", "SELECT 1;")
    write(tmp_path, "002_second.sql", "SELECT 2;")
    files = discover(tmp_path)

    assert [file.version for file in pending(files, {"001": files[0].sha256})] == ["002"]
    assert pending(files, {"001": files[0].sha256, "002": files[1].sha256}) == []

    with pytest.raises(MigrationDrift, match="changed after it was applied"):
        pending(files, {"001": "edited"})
    with pytest.raises(MigrationDrift, match="missing in db/"):
        pending(files, {"003": "x"})


def test_new_file_older_than_applied_ones_is_refused(tmp_path: Path):
    write(tmp_path, "001_first.sql", "SELECT 1;")
    write(tmp_path, "002_second.sql", "SELECT 2;")
    files = discover(tmp_path)

    with pytest.raises(MigrationDrift, match="older than the latest applied"):
        pending(files, {"002": files[1].sha256})


@pytest.mark.db
async def test_repository_migrations_apply_once_on_a_fresh_database():
    """Needs LUDIK_TEST_DSN pointing to an empty scratch database on PostgreSQL with TimescaleDB."""
    dsn = os.environ.get("LUDIK_TEST_DSN")
    if not dsn:
        pytest.skip("LUDIK_TEST_DSN is not set")
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        # Other db tests may have migrated the scratch database already; the end state is what matters.
        await migrate(connection, REPO_ROOT / "db")
        again = await migrate(connection, REPO_ROOT / "db")
        recorded = [row["filename"] for row in await connection.fetch("SELECT filename FROM schema_migrations ORDER BY version")]
        hypertables = await connection.fetchval("SELECT count(*) FROM timescaledb_information.hypertables")
    finally:
        await connection.close()

    # Whatever db/ holds: a new migration must not make this test a chore to update.
    assert recorded == sorted(path.name for path in (REPO_ROOT / "db").glob("*.sql"))
    assert again == []
    assert hypertables == 5
