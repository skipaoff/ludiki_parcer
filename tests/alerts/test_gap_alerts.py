"""A gap that becomes clickable is announced once, and a restart does not announce the whole screen."""

from app.alerts.service import GapAlerts
from app.core.alerts import AlertRules
from app.journal.journal import Journal


def row(key="binance:SOLUSDT|mexc:SOL_USDT", token="SOL", blocks=(), lifetime_ms=120_000):
    return {
        "key": key,
        "token": token,
        "lifetime_ms": lifetime_ms,
        "long": {"exchange": "mexc", "symbol": "SOL_USDT"},
        "short": {"exchange": "binance", "symbol": "SOLUSDT"},
        "total_pct": "1.4000",
        "roi_net_pct": "1.3000",
        "score": 64,
        "size_usd": "100.00",
        "block": None,
        "open_blocks": list(blocks),
    }


class Harness:
    def __init__(self, rows):
        self.rows = rows
        self.now = 1_000_000.0
        self.events = []
        journal = Journal()
        journal.add_sink(self.events.append)
        self.alerts = GapAlerts(lambda: self.rows, journal, AlertRules(cooldown_ms=600_000), clock_ms=lambda: self.now)

    def types(self):
        return [(event.type, event.payload.get("token")) for event in self.events]


def test_one_event_per_gap_with_the_numbers_of_its_row():
    h = Harness([row()])

    assert h.alerts.check() == ["binance:SOLUSDT|mexc:SOL_USDT"]
    event = h.events[-1]
    assert event.source == "feed" and event.type == "gap_actionable"
    assert event.payload["token"] == "SOL" and event.payload["long"] == "mexc" and event.payload["short"] == "binance"
    assert event.payload["total_pct"] == "1.4000" and event.payload["interest"] == 64
    assert event.payload["blocks"] == []

    h.now += 5_000
    assert h.alerts.check() == [] and len(h.events) == 1
    assert h.alerts.sent == 1


def test_trading_being_off_is_announced_as_a_reason_not_as_silence():
    h = Harness([row(blocks=["trading_disabled"])])

    assert h.alerts.check() != []
    assert h.events[-1].payload["blocks"] == ["trading_disabled"]


def test_gaps_already_on_screen_at_startup_are_remembered_without_being_announced():
    h = Harness([row(), row(key="other", token="ABC")])

    h.alerts._catch_up()

    assert h.events == []
    h.now += 1_000
    assert h.alerts.check() == []

    h.rows = h.rows + [row(key="new", token="XYZ")]
    assert h.alerts.check() == ["new"]
    assert h.types() == [("gap_actionable", "XYZ")]


def test_a_broken_row_does_not_take_the_loop_down():
    h = Harness([{"key": "broken", "lifetime_ms": 120_000}])

    assert h.alerts.check() == ["broken"]  # missing numbers are announced as empty, not as a crash
    assert h.events[-1].payload["total_pct"] is None


def test_a_gap_that_only_flickered_never_reaches_the_journal():
    """Вилка живёт 3 секунды — это задержка биржи, а не возможность."""
    h = Harness([row(lifetime_ms=3_000)])

    assert h.alerts.check() == [] and h.events == []
