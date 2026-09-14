import csv
import io
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config.settings import REPO_ROOT
from app.storage.trades_history import TradeFilter


def trade_row(trade_id, token, pnl, expected, actual, opened, minutes, status="closed", long="mexc", short="binance"):
    return {
        "id": trade_id, "strategy": "price_gap", "episode_id": None, "pair_id": None, "token": token,
        "long_exchange": long, "short_exchange": short, "qty_tokens": Decimal("10"), "size_usd": Decimal("1000"),
        "leverage_long": 3, "leverage_short": 3, "margin_mode": "isolated", "status": status,
        "opened_at": opened, "closed_at": opened + timedelta(minutes=minutes), "roi_expected_entry": expected,
        "roi_actual_entry": actual, "fees_usd": Decimal("2"), "funding_usd": Decimal("0.1"), "pnl_net_usd": pnl,
        "pnl_net_pct": pnl / 10, "close_reason": "manual", "click_to_fill_ms_long": 300, "click_to_fill_ms_short": 450,
        "settings_snapshot": {"legs": {"long_symbol": "X", "short_symbol": "Y"}},
    }


@pytest.mark.db
async def test_statistics_reconcile_with_raw_trades():
    """Needs LUDIK_TEST_DSN pointing to a scratch database on PostgreSQL with TimescaleDB."""
    dsn = os.environ.get("LUDIK_TEST_DSN")
    if not dsn:
        pytest.skip("LUDIK_TEST_DSN is not set")
    import asyncpg

    from app.storage import trades_history
    from app.storage.database import Database, _init_connection
    from app.storage.migrations import migrate

    database = Database.__new__(Database)
    database._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, init=_init_connection)
    base = datetime.now(UTC) - timedelta(hours=3)
    rows = [
        trade_row(9001, "SOL", Decimal("8.5"), Decimal("1.0"), Decimal("0.9"), base, 30),
        trade_row(9002, "SOL", Decimal("-3.2"), Decimal("0.7"), Decimal("0.4"), base + timedelta(minutes=40), 12),
        trade_row(9003, "PEPE", Decimal("4.0"), Decimal("0.8"), Decimal("0.8"), base + timedelta(hours=1), 60, long="binance", short="mexc"),
        trade_row(9004, "BTC", Decimal("-0.5"), Decimal("0.6"), None, base + timedelta(hours=2), 1, status="leg_failed"),
    ]
    try:
        async with database.pool.acquire() as connection:
            await migrate(connection, REPO_ROOT / "db")
            await connection.execute("DELETE FROM trades WHERE id BETWEEN 9001 AND 9004")
        await database.insert_batch([("trades", row) for row in rows])
        flt = TradeFilter(base - timedelta(minutes=1), datetime.now(UTC) + timedelta(days=1))

        stats = await trades_history.trade_stats(database.pool, flt)
        raw = await database.pool.fetchrow(
            "SELECT count(*) AS n, sum(pnl_net_usd) AS pnl, count(*) FILTER (WHERE pnl_net_usd > 0) AS wins "
            "FROM trades WHERE id BETWEEN 9001 AND 9004"
        )

        assert stats["totals"]["trades"] == raw["n"] == 4
        assert Decimal(stats["totals"]["pnl_net_usd"]) == raw["pnl"] == Decimal("8.8")
        assert stats["totals"]["wins"] == raw["wins"] == 2 and stats["totals"]["failed"] == 1
        assert stats["totals"]["win_rate"] == 0.5
        slippage = {(row["long_exchange"], row["short_exchange"]): row for row in stats["slippage"]}
        assert Decimal(slippage[("mexc", "binance")]["avg_slippage_pct"]) == Decimal("0.2")  # (0.1 + 0.3) / 2
        latency = {row["exchange"]: row for row in stats["latency"]}
        assert float(latency["mexc"]["median_ms"]) in (300.0, 450.0, 375.0)
        assert {row["token"]: Decimal(row["pnl_net_usd"]) for row in stats["by_token"]}["SOL"] == Decimal("5.3")
        assert sum(row["trades"] for row in stats["by_hour"]) == 4

        sol_only = await trades_history.trades(database.pool, TradeFilter(flt.since, flt.until, token="SOL"), 100)
        assert [row["id"] for row in sol_only] == ["9002", "9001"]

        exported = list(csv.reader(io.StringIO(await trades_history.trades_csv(database.pool, flt))))
        assert exported[0][:3] == ["id", "token", "long_exchange"]
        assert len(exported) == 5
    finally:
        async with database.pool.acquire() as connection:
            await connection.execute("DELETE FROM trades WHERE id BETWEEN 9001 AND 9004")
        await database.pool.close()
