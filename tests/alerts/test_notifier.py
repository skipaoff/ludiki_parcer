"""What reaches the phone: the thresholds, the quiet window, the ceiling, and the outcome of a gap."""

from datetime import datetime
from decimal import Decimal

from app.alerts.notifier import TelegramNotifier, parse_quiet_hours
from app.config.settings import TelegramSettings
from app.core.alerts import AlertLeg, FinishedAlert, GapAlert
from app.journal.journal import JournalEvent, Level


class FakeSender:
    def __init__(self):
        self.posts: list = []
        self.amends: list = []
        self.message_ids: dict[str, int] = {}

    def post(self, message, key=None):
        self.posts.append((message, key))
        if key:
            self.message_ids[key] = (("480399842", 100 + len(self.posts)), ("944522988", 200 + len(self.posts)))

    def amend(self, sent, text):
        self.amends.append((sent, text))

    def stats(self):
        return {"sent": len(self.posts)}


def alert(key="pair", token="SOL", total="1.6700", interest=73) -> GapAlert:
    return GapAlert(
        key=key,
        token=token,
        long=AlertLeg("gate", "SOL_USDT", "https://gate.example", "142.10"),
        short=AlertLeg("mexc", "SOL_USDT", "https://mexc.example", "143.05"),
        total_pct=total,
        profit_pct="1.5900",
        profit_usd="1.59",
        interest=interest,
        size_usd="100.00",
    )


def notifier(clock=datetime(2026, 9, 26, 12, 0), **settings):
    sender = FakeSender()
    made = TelegramNotifier(
        sender,
        TelegramSettings(enabled=True, chats=("480399842", "944522988"), **settings),
        horizon_h=lambda: "8",
        now=lambda: clock,
    )
    return made, sender


def test_quiet_hours_parse_and_cover_a_window_across_midnight():
    window = parse_quiet_hours("23:00-08:00")

    assert window.covers(23 * 60) and window.covers(2 * 60) and window.covers(7 * 60 + 59)
    assert not window.covers(8 * 60) and not window.covers(12 * 60)
    assert parse_quiet_hours("") is None


def test_a_gap_above_the_thresholds_is_sent_with_its_buttons():
    made, sender = notifier()
    made.on_alerts([alert()])

    message, key = sender.posts[0]
    assert key == "pair" and "<b>SOL</b>" in message.text
    assert message.buttons[0][1] == "https://gate.example"


def test_a_gap_below_the_phone_threshold_stays_on_the_screen_only():
    made, sender = notifier(min_interest=80, min_total_pct=Decimal("1.0"))
    made.on_alerts([alert(interest=73)])

    assert sender.posts == [] and made.skipped_threshold == 1


def test_nothing_buzzes_during_quiet_hours():
    made, sender = notifier(clock=datetime(2026, 9, 26, 2, 30), quiet_hours="23:00-08:00")
    made.on_alerts([alert()])

    assert sender.posts == [] and made.skipped_quiet == 1


def test_a_storm_of_gaps_stops_at_the_hourly_ceiling():
    made, sender = notifier(max_messages_per_hour=2)
    made.on_alerts([alert(key="a"), alert(key="b"), alert(key="c")])

    assert len(sender.posts) == 2 and made.skipped_rate == 1


def test_the_best_gap_of_the_day_is_marked_only_once_it_beats_another():
    made, sender = notifier()
    made.on_alerts([alert(key="a", total="1.0000")])
    made.on_alerts([alert(key="b", total="2.0000")])
    made.on_alerts([alert(key="c", total="1.5000")])

    assert not sender.posts[0][0].text.startswith("🔥")
    assert sender.posts[1][0].text.startswith("🔥")
    assert not sender.posts[2][0].text.startswith("🔥")


def test_the_outcome_is_appended_to_the_message_that_announced_the_gap():
    made, sender = notifier()
    made.on_alerts([alert(key="pair")])
    message_id = sender.message_ids["pair"]

    made.on_finished([FinishedAlert(key="pair", lifetime_ms=420_000, peak_total_pct="2.1000")])

    amended, text = sender.amends[0]
    assert amended == message_id
    assert text.startswith(sender.posts[0][0].text)
    assert "✓ прожила 7 мин · максимум был +2.10%" in text
    assert "pair" not in sender.message_ids  # больше нечего дописывать


def test_an_outcome_nobody_announced_is_not_invented():
    made, sender = notifier()
    made.on_finished([FinishedAlert(key="unknown", lifetime_ms=1000, peak_total_pct=None)])

    assert sender.amends == []


def test_outcomes_can_be_switched_off():
    made, sender = notifier(send_outcome=False)
    made.on_alerts([alert(key="pair")])
    made.on_finished([FinishedAlert(key="pair", lifetime_ms=1000, peak_total_pct=None)])

    assert sender.amends == []


def event(kind: str, level: Level = Level.CRITICAL, **payload) -> JournalEvent:
    return JournalEvent(ts=datetime.now(), level=level, source="test", type=kind, payload=payload)


def test_alarms_reach_the_phone_even_at_night():
    made, sender = notifier(clock=datetime(2026, 9, 26, 3, 0), quiet_hours="23:00-08:00")
    made.on_event(event("leg_lost", trade_id=7))

    assert "нога пары осталась открытой" in sender.posts[0][0].text


def test_routine_events_are_not_alarms():
    made, sender = notifier()
    made.on_event(event("gap_actionable", level=Level.INFO))
    made.on_event(event("check_ok", level=Level.INFO))

    assert sender.posts == []


def test_alarms_can_be_switched_off():
    made, sender = notifier(alarms=False)
    made.on_event(event("db_unavailable"))

    assert sender.posts == []
