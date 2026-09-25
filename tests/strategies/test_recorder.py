import os
from decimal import Decimal

import pytest

from app.config.settings import REPO_ROOT
from app.core.funding import FundingRate
from app.core.pairs import Quote, assess_pair
from app.market.radar_recorder import snapshot_rows
from app.market.state import MarketState
from app.storage.ids import LocalIds
from app.strategies.price_gap.recorder import EpisodeRecorder
from tests.strategies.test_price_gap_engine import build, push_market, sol_record


def test_local_ids_are_unique_and_increasing_within_one_millisecond():
    ids = LocalIds(clock_ms=lambda: 1_789_300_000_000)
    issued = [ids.next() for _ in range(5000)]
    assert issued == sorted(issued)
    assert len(set(issued)) == 5000
    assert issued[0] >> 12 == 1_789_300_000_000


def record_engine():
    rows = []
    recorder = EpisodeRecorder(lambda table, row: rows.append((table, row)), lambda: {"size_usd": "1000"})
    engine, state, clock, binance, mexc, _ = build(sol_record())
    engine._sink = recorder
    return engine, state, clock, binance, mexc, recorder, rows


def test_gap_history_is_written_on_entry_every_second_and_on_end():
    engine, state, clock, binance, mexc, recorder, rows = record_engine()
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()  # enters the feed

    episodes = [row for table, row in rows if table == "opportunity_episodes"]
    assert len(episodes) == 1
    first = episodes[0]
    assert first["long_exchange"] == "mexc" and first["short_exchange"] == "binance"
    assert first["ended_at"] is None and first["roi_first"] == Decimal("1.0")

    for _ in range(3):
        clock.now += 1_000
        push_market(state)
        engine.tick()
    samples = [row for table, row in rows if table == "episode_samples"]
    assert len(samples) >= 3
    assert {sample["episode_id"] for sample in samples} == {first["id"]}
    assert samples[-1]["roi_net_pct"] == Decimal("1.0")
    assert samples[-1]["bid_long_exit_vwap"] is not None

    engine.close()
    final = [row for table, row in rows if table == "opportunity_episodes"][-1]
    assert final["id"] == first["id"]
    assert final["end_reason"] == "app_stop" and final["ended_at"] is not None
    assert final["samples_count"] == len(samples)
    assert set(final) == set(first)  # same columns, so the write queue collapses both into one upsert
    assert recorder.open_count == 0


def test_funding_and_interest_of_a_gap_are_recorded_next_to_its_spread():
    """History must hold the numbers the screen decided on, or thresholds get calibrated on a different gap."""
    engine, state, clock, binance, mexc, recorder, rows = record_engine()
    rate = FundingRate(rate_pct=Decimal("-0.01"), interval_hours=Decimal("8"), next_ms=None)
    engine._funding = lambda exchange, symbol: rate
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()
    for _ in range(2):
        clock.now += 1_000
        push_market(state)
        engine.tick()

    sample = [row for table, row in rows if table == "episode_samples"][-1]
    assert sample["funding_hourly_pct"] == Decimal("0")  # both legs pay the same rate, so the pair is flat on funding
    assert sample["funding_horizon_pct"] == Decimal("0")
    assert sample["total_pct"] == sample["roi_net_pct"] == Decimal("1.0")
    assert 0 < sample["interest"] <= 100

    engine.close()
    final = [row for table, row in rows if table == "opportunity_episodes"][-1]
    assert final["total_peak_pct"] == Decimal("1.0") and final["total_peak_at"] is not None
    assert final["interest_peak"] == sample["interest"]
    assert final["funding_horizon_pct_first"] == Decimal("0")


def test_a_gap_without_funding_rates_records_the_spread_and_leaves_funding_empty():
    engine, state, clock, binance, mexc, recorder, rows = record_engine()
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()
    clock.now += 1_000
    push_market(state)
    engine.tick()

    sample = [row for table, row in rows if table == "episode_samples"][-1]
    assert sample["funding_horizon_pct"] is None and sample["funding_next_at"] is None
    assert sample["total_pct"] == Decimal("1.0") and sample["interest"] is not None


def test_gap_of_a_suspicious_pair_is_counted_but_not_recorded():
    engine, state, clock, binance, mexc, recorder, rows = record_engine()
    record = engine._catalog.records()[0]
    record.assessment = assess_pair(
        record.assessment.a, record.assessment.b, Quote(mark=Decimal("100"), index=Decimal("100")), Quote(mark=Decimal("100"), index=Decimal("104"))
    )
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()

    assert len(engine.view()["rows"]) == 1
    assert rows == [] and recorder.skipped_suspicious == 1


def test_price_mismatch_pair_never_reaches_the_radar():
    engine, state, clock, *_ = record_engine()
    record = engine._catalog.records()[0]
    record.assessment = assess_pair(record.assessment.a, record.assessment.b, Quote(mark=Decimal("100")), Quote(mark=Decimal("150"), index=Decimal("150")))
    engine.tick()
    push_market(state)
    engine.tick()
    assert engine.view()["stats"]["radar_pairs"] == 0

    record.manually_verified = True
    engine.tick()
    assert engine.view()["stats"]["radar_pairs"] == 1


def test_candidate_that_never_reaches_the_feed_is_not_recorded():
    engine, state, clock, *_, rows = record_engine()
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 100
    push_market(state, mexc_ask="100.00", binance_bid="100.01")
    engine.tick()
    assert rows == []


def test_radar_snapshot_rows_need_catalog_ids_and_fresh_prices():
    record = sol_record(instrument_a_id=11, instrument_b_id=22)
    state = MarketState(lambda: 10_000_000.0)
    state.set_instruments([record.assessment.a, record.assessment.b])
    state.set_top("binance", "SOLUSDT", 100.0, 100.1, 1)
    state.set_mark("mexc", "SOL_USDT", 100.05, 100.02, 5_000_000.0)

    rows = snapshot_rows([record, sol_record()], state, now_ms=10_000_000.0)

    assert [row["instrument_id"] for row in rows] == [11, 22]
    assert rows[0]["bid"] == 100.0 and rows[0]["mark"] is None
    assert rows[1]["bid"] is None and rows[1]["index_price"] == 100.02 and rows[1]["volume24h_usd"] == 5_000_000.0
    assert rows[0]["funding_rate_pct"] is None  # no rates given: the column says "unknown", not "zero"


def test_radar_snapshot_keeps_the_funding_rate_of_each_contract():
    """Holding cost is funding; without it a backtest on this history could only price the entry."""
    record = sol_record(instrument_a_id=11, instrument_b_id=22)
    state = MarketState(lambda: 10_000_000.0)
    state.set_instruments([record.assessment.a, record.assessment.b])
    rate = FundingRate(rate_pct=Decimal("0.01"), interval_hours=Decimal("4"), next_ms=10_800_000)

    rows = snapshot_rows([record], state, now_ms=10_000_000.0, funding=lambda exchange, symbol: rate if exchange == "binance" else None)

    binance_row = next(row for row in rows if row["instrument_id"] == 11)
    mexc_row = next(row for row in rows if row["instrument_id"] == 22)
    assert binance_row["funding_rate_pct"] == Decimal("0.01") and binance_row["funding_interval_h"] == Decimal("4")
    assert binance_row["funding_next_at"] is not None
    assert mexc_row["funding_rate_pct"] is None and mexc_row["funding_next_at"] is None


@pytest.mark.db
async def test_episode_and_its_samples_land_in_one_batch_with_upsert():
    """Needs LUDIK_TEST_DSN pointing to a scratch database on PostgreSQL with TimescaleDB."""
    dsn = os.environ.get("LUDIK_TEST_DSN")
    if not dsn:
        pytest.skip("LUDIK_TEST_DSN is not set")
    import asyncpg

    from app.storage.database import Database
    from app.storage.migrations import migrate

    engine, state, clock, binance, mexc, recorder, rows = record_engine()
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()
    for _ in range(2):
        clock.now += 1_000
        push_market(state)
        engine.tick()
    engine.close()

    database = Database.__new__(Database)
    database._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with database.pool.acquire() as connection:
            await migrate(connection, REPO_ROOT / "db")
        await database.insert_batch(rows)
        episode_id = rows[0][1]["id"]
        stored = await database.pool.fetchrow(
            "SELECT end_reason, samples_count, (SELECT count(*) FROM episode_samples WHERE episode_id = $1) AS samples "
            "FROM opportunity_episodes WHERE id = $1",
            episode_id,
        )
        assert stored["end_reason"] == "app_stop"
        assert stored["samples"] == stored["samples_count"] >= 2
    finally:
        await database.pool.close()
