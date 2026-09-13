from datetime import UTC, datetime

from app.journal.journal import Journal, Level


def fixed_clock():
    return datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def test_event_reaches_every_sink_even_if_one_fails():
    journal = Journal(clock=fixed_clock)
    received = []

    def broken(event):
        raise RuntimeError("sink down")

    journal.add_sink(broken)
    journal.add_sink(received.append)

    event = journal.emit(Level.WARNING, "storage", "db_unavailable", error="refused")

    assert received == [event]
    assert event.to_row() == {
        "ts": fixed_clock(),
        "level": "warning",
        "source": "storage",
        "type": "db_unavailable",
        "exchange": None,
        "trade_id": None,
        "payload": {"error": "refused"},
    }
    assert event.to_wire()["ts_ms"] == 1789300800000


def test_recent_keeps_only_the_newest_events():
    journal = Journal(buffer_size=3, clock=fixed_clock)
    for index in range(5):
        journal.emit(Level.INFO, "app", f"e{index}")

    assert [event.type for event in journal.recent(10)] == ["e2", "e3", "e4"]
    assert [event.type for event in journal.recent(2)] == ["e3", "e4"]
    assert journal.recent(0) == []
