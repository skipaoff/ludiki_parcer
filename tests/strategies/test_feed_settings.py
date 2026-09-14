from decimal import Decimal

import pytest

from app.journal.journal import Journal
from app.strategies.price_gap.settings import FeedSettingsService, InvalidSetting, parse_changes
from tests.strategies.test_price_gap_engine import build, sol_record


class NoDatabase:
    ready = False


def test_parse_accepts_numbers_in_range_and_refuses_the_rest():
    assert parse_changes({"size_usd": "250", "min_roi_pct": 0.3}) == {"size_usd": Decimal("250"), "min_roi_pct": Decimal("0.3")}
    with pytest.raises(InvalidSetting, match="cannot be changed"):
        parse_changes({"book_limit": 5})
    with pytest.raises(InvalidSetting, match="between"):
        parse_changes({"size_usd": "1"})
    with pytest.raises(InvalidSetting, match="number"):
        parse_changes({"min_roi_pct": "abc"})
    with pytest.raises(InvalidSetting):
        parse_changes({"min_roi_pct": "NaN"})
    assert parse_changes({"enter_after_ms": "45000"}) == {"enter_after_ms": 45000}
    with pytest.raises(InvalidSetting, match="whole"):
        parse_changes({"enter_after_ms": "4500.5"})


async def test_lower_threshold_lets_a_smaller_gap_into_the_feed():
    engine, state, clock, *_ = build(sol_record())
    journal = Journal()
    events = []
    journal.add_sink(events.append)
    service = FeedSettingsService(engine, NoDatabase(), journal)

    from tests.strategies.test_price_gap_engine import push_market

    def run_gap(bid):
        engine.tick()
        push_market(state, mexc_ask="100.00", binance_bid=bid)
        engine.tick()
        clock.now += 1_100
        push_market(state, mexc_ask="100.00", binance_bid=bid)
        engine.tick()

    run_gap("100.40")  # 0.4 % gross, 0.2 % net — below the 0.5 % default
    assert engine.view()["rows"] == []

    assert await service.update({"min_roi_pct": "0.1"}) == {"size_usd": "1000", "min_roi_pct": "0.1", "enter_after_ms": "300"}
    clock.now += 1_100
    run_gap("100.40")

    assert len(engine.view()["rows"]) == 1
    assert [(event.type, event.payload["new"]) for event in events] == [("changed", "0.1")]
