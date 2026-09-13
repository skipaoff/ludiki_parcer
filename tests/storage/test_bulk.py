import os
from datetime import UTC, datetime
from decimal import Decimal

import orjson
import pytest

from app.config.settings import REPO_ROOT
from app.storage.bulk import BulkWriteError, affected_rows, bulk_insert, insert_select_sql, rows_json


def test_insert_select_casts_through_the_table_row_type():
    sql = insert_select_sql("pairs", ("token", "suspicious"), {"updated_at": "now()"}, "ON CONFLICT DO NOTHING")
    assert sql == (
        'INSERT INTO "pairs" ("token", "suspicious", "updated_at") '
        'SELECT "token", "suspicious", now() FROM jsonb_populate_recordset(NULL::"pairs", $1::text::jsonb) '
        "ON CONFLICT DO NOTHING"
    )


def test_unsafe_identifiers_are_refused():
    with pytest.raises(ValueError):
        insert_select_sql('events"; DROP TABLE trades; --', ("ts",))
    with pytest.raises(ValueError):
        insert_select_sql("events", ("Ts",))


def test_values_keep_full_precision_in_json():
    text = rows_json([{"roi": Decimal("0.123456789012345678901"), "ts": datetime(2026, 9, 13, 12, 0, tzinfo=UTC), "p": {"a": 1}}])
    assert orjson.loads(text) == [{"roi": "0.123456789012345678901", "ts": "2026-09-13T12:00:00+00:00", "p": {"a": 1}}]


def test_command_status_is_parsed():
    assert affected_rows("INSERT 0 42") == 42
    with pytest.raises(BulkWriteError):
        affected_rows("SELECT")


class ShortConnection:
    def __init__(self, stored: int):
        self.stored = stored
        self.calls = []

    async def execute(self, sql, payload):
        self.calls.append((sql, payload))
        return f"INSERT 0 {self.stored}"


async def test_a_short_write_raises_instead_of_passing():
    rows = [{"n": 1}, {"n": 2}, {"n": 3}]
    with pytest.raises(BulkWriteError, match="sent 3 rows, database stored 2"):
        await bulk_insert(ShortConnection(stored=2), "events", rows)
    assert await bulk_insert(ShortConnection(stored=3), "events", rows) == 3
    assert await bulk_insert(ShortConnection(stored=0), "events", []) == 0


async def test_rows_with_different_columns_are_refused():
    with pytest.raises(ValueError):
        await bulk_insert(ShortConnection(stored=2), "events", [{"a": 1}, {"b": 2}])


@pytest.mark.db
async def test_events_and_catalog_round_trip_on_a_real_database():
    """Needs LUDIK_TEST_DSN pointing to a scratch database on PostgreSQL with TimescaleDB."""
    dsn = os.environ.get("LUDIK_TEST_DSN")
    if not dsn:
        pytest.skip("LUDIK_TEST_DSN is not set")
    import asyncpg

    from app.core.pairs import assess_pair, match_instruments
    from app.exchanges.binance import adapter as binance
    from app.exchanges.mexc import adapter as mexc
    from app.storage.catalog import save_catalog, set_pair_flags
    from app.storage.migrations import migrate
    from tests.exchanges.test_instruments_parsing import load

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            await migrate(connection, REPO_ROOT / "db")
            events = [
                {"ts": datetime(2026, 9, 13, 12, 0, second, tzinfo=UTC), "level": "warning", "source": "test", "type": "bulk",
                 "exchange": None, "trade_id": None, "payload": {"n": second, "roi": Decimal("0.5")}}
                for second in range(50)
            ]
            assert await bulk_insert(connection, "events", events) == 50
            stored = await connection.fetchval("SELECT count(*) FROM events WHERE source = 'test' AND type = 'bulk'")
            assert stored == 50

        instruments_a = binance.parse_instruments(load("binance_exchangeinfo"))
        instruments_b = mexc.parse_instruments(load("mexc_detail"))
        quotes_a = binance.parse_quotes(load("binance_premiumindex"), load("binance_bookticker"), load("binance_ticker24h"))
        quotes_b = mexc.parse_quotes(load("mexc_ticker"))
        assessments = [
            assess_pair(a, b, quotes_a.get(a.symbol_raw), quotes_b.get(b.symbol_raw))
            for a, b in match_instruments(instruments_a, instruments_b)
        ]

        first = await save_catalog(pool, instruments_a + instruments_b, assessments)
        one_key = next(key for key in first if key.startswith("binance:ONEUSDT"))
        await set_pair_flags(pool, first[one_key].pair_id, manually_verified=True, blacklisted=None)
        second = await save_catalog(pool, instruments_a + instruments_b, assessments)

        assert len(first) == len(assessments) == 5
        assert second[one_key].manually_verified is True
        assert second[one_key].pair_id == first[one_key].pair_id
    finally:
        await pool.close()
