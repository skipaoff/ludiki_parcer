from decimal import Decimal

import pytest

from app.config.settings import PortfolioSettings
from app.core.schemas import LegSide
from app.exchanges.base import Balance, Position
from app.journal.journal import Journal
from app.market.state import MarketState
from app.portfolio.service import PortfolioError, PortfolioService
from tests.strategies.test_price_gap_engine import Catalog, sol_record


class Clock:
    def __init__(self):
        self.now = 1_789_300_000_000.0

    def __call__(self):
        return self.now


class FakeAdapter:
    def __init__(self, positions):
        self.positions = positions
        self.fail = False

    async def fetch_positions(self, instruments):
        if self.fail:
            raise ConnectionError("timeout")
        return [position for position in self.positions if position.symbol_raw in instruments]

    async def fetch_balance(self):
        return Balance("x", Decimal("1000"), Decimal("900"), Decimal("100"))

    async def fetch_funding_usd(self, symbol, since_ms):
        return Decimal("0.1")


class FakeExchanges:
    def __init__(self, adapters):
        self.adapters = adapters

    def snapshot(self):
        return [{"name": name, "keys": "ok"} for name in self.adapters]

    def adapter(self, name):
        return self.adapters[name]


class NoDatabase:
    ready = False


def leg(exchange, symbol, side, qty, entry, liquidation):
    return Position(exchange, symbol, "SOL", side, Decimal(qty), Decimal(entry), None, Decimal(liquidation), 3, "isolated")


def build(positions_binance, positions_mexc):
    clock = Clock()
    state = MarketState(lambda: clock.now)
    record = sol_record(pair_id=5)
    state.set_instruments([record.assessment.a, record.assessment.b])
    rows, events, active = [], [], []
    journal = Journal()
    journal.add_sink(events.append)
    adapters = {"binance": FakeAdapter(positions_binance), "mexc": FakeAdapter(positions_mexc)}
    service = PortfolioService(
        FakeExchanges(adapters),
        Catalog([record]),
        state,
        NoDatabase(),
        lambda table, row: rows.append((table, row)),
        journal,
        PortfolioSettings(),
        lambda exchange: Decimal("0.05"),
        on_active_change=active.append,
        clock_ms=clock,
    )
    return service, state, clock, adapters, rows, events, active, record


async def test_manual_legs_are_foreign_until_assigned_then_monitored():
    long = leg("mexc", "SOL_USDT", LegSide.LONG, "10.05", "100", "70")
    short = leg("binance", "SOLUSDT", LegSide.SHORT, "10", "101.2", "130")
    service, state, clock, adapters, rows, events, active, record = build([short], [long])

    await service.poll_positions()
    snapshot = service.snapshot()
    assert len(snapshot["foreign"]) == 2
    assert snapshot["suggestions"][0]["token"] == "SOL"

    trade = await service.assign_pair("mexc", "SOL_USDT", "binance", "SOLUSDT")

    assert trade["qty_tokens"] == "10"  # floored to the common 0.1 SOL step
    assert service.snapshot()["foreign"] == []
    assert service.pinned_pair_keys() == [record.key]
    assert active == [True]
    trade_rows = [row for table, row in rows if table == "trades"]
    assert trade_rows[0]["status"] == "open" and trade_rows[0]["pair_id"] == 5
    assert trade_rows[0]["settings_snapshot"]["legs"] == {"long_symbol": "SOL_USDT", "short_symbol": "SOLUSDT"}

    # Books and marks arrive: PnL and liquidation distance appear.
    state.set_book("mexc", "SOL_USDT", [[101.0, 1000, 1]], [[101.1, 1000, 1]], 1)
    state.set_book("binance", "SOLUSDT", [["101.1", "100"]], [["101.2", "100"]], 1)
    state.set_mark("mexc", "SOL_USDT", 101.0, 101.0)
    state.set_mark("binance", "SOLUSDT", 101.1, 101.0)
    clock.now += 1000
    service.tick()

    card = service.snapshot()["trades"][0]
    assert Decimal(card["entry_spread_pct"]) == Decimal("1.2")
    assert Decimal(card["pnl_now_usd"]) < Decimal("10")
    # Worst leg is the short: mark 101.1 against liquidation 130 is 28.59 % away (the long is 30.69 % away).
    assert Decimal(card["liq_worst_pct"]) == Decimal("28.59")
    assert any(table == "position_samples" for table, _ in rows)


async def test_leg_lost_needs_two_polls_and_a_failed_poll_is_not_a_loss():
    long = leg("mexc", "SOL_USDT", LegSide.LONG, "10", "100", "70")
    short = leg("binance", "SOLUSDT", LegSide.SHORT, "10", "101.2", "130")
    service, state, clock, adapters, rows, events, active, _ = build([short], [long])
    await service.poll_positions()
    await service.assign_pair("mexc", "SOL_USDT", "binance", "SOLUSDT")

    adapters["binance"].fail = True
    await service.poll_positions()
    await service.poll_positions()
    assert service.snapshot()["trades"][0]["status"] == "open"

    adapters["binance"].fail = False
    adapters["binance"].positions = []  # closed by hand on the exchange
    await service.poll_positions()
    assert service.snapshot()["trades"][0]["status"] == "open"
    await service.poll_positions()
    assert service.snapshot()["trades"][0]["status"] == "leg_lost"
    assert [event.type for event in events if event.level.value == "critical"] == ["leg_lost"]

    trade_id = int(service.snapshot()["trades"][0]["id"])
    await service.close_record(trade_id)
    assert service.snapshot()["trades"] == []
    assert active == [True, False]
    assert [row for table, row in rows if table == "trades"][-1]["close_reason"] == "external"


@pytest.mark.db
async def test_assigned_pair_survives_a_restart():
    """Needs LUDIK_TEST_DSN pointing to a scratch database on PostgreSQL with TimescaleDB."""
    import os

    dsn = os.environ.get("LUDIK_TEST_DSN")
    if not dsn:
        pytest.skip("LUDIK_TEST_DSN is not set")
    import asyncpg

    from app.config.settings import REPO_ROOT
    from app.storage.database import Database, _init_connection
    from app.storage.migrations import migrate

    long = leg("mexc", "SOL_USDT", LegSide.LONG, "10", "100", "70")
    short = leg("binance", "SOLUSDT", LegSide.SHORT, "10", "101.2", "130")
    service, state, clock, adapters, rows, events, active, record = build([short], [long])
    await service.poll_positions()
    trade = await service.assign_pair("mexc", "SOL_USDT", "binance", "SOLUSDT")

    database = Database.__new__(Database)
    database._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, init=_init_connection)
    try:
        async with database.pool.acquire() as connection:
            await migrate(connection, REPO_ROOT / "db")
        # The scratch database has no catalog, so the trade is stored without its pair reference.
        await database.insert_batch([(table, {**row, "pair_id": None}) for table, row in rows if table == "trades"])

        restarted, *_ = build([short], [long])
        restarted._database = database
        assert await restarted.load() >= 1
        restored = restarted.trades[int(trade["id"])]
        assert (restored.long_symbol, restored.short_symbol, restored.qty_tokens) == ("SOL_USDT", "SOLUSDT", Decimal("10"))
        await restarted.poll_positions()
        assert restarted.snapshot()["foreign"] == []
    finally:
        await database.pool.close()


async def test_assign_refuses_legs_that_are_not_a_hedged_pair():
    long = leg("mexc", "SOL_USDT", LegSide.LONG, "10", "100", "70")
    also_long = leg("binance", "SOLUSDT", LegSide.LONG, "10", "101", "70")
    service, *_ = build([also_long], [long])
    await service.poll_positions()
    with pytest.raises(PortfolioError):
        await service.assign_pair("mexc", "SOL_USDT", "binance", "SOLUSDT")
