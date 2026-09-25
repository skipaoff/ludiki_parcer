"""
VFP: Decides which announcements reach the phone, in what words, and how often — then hands them to the sender.
Changes when: the rules for what deserves a message on the phone change.
Anti-goal:
1. A second opinion about what a gap is worth: the feed decided, this only narrows by threshold, hour and rate.
2. A night of buzzing: quiet hours and an hourly ceiling are part of the contract, not an afterthought.
3. Losing the outcome: the message that announced a gap is the one amended when the gap ends.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from app.core.alerts import FinishedAlert, GapAlert, PhoneGate, passes
from app.core.telegram_message import DigestNumbers, alarm, closed_note, digest, gap
from app.journal.journal import JournalEvent, Level

log = logging.getLogger(__name__)

ALARM_TYPES = frozenset(
    {"exchange_down", "exchange_up", "db_unavailable", "db_connected", "leg_lost", "liquidation_near", "started", "stopped"}
)


@dataclass(frozen=True, slots=True)
class QuietHours:
    start_minute: int
    end_minute: int

    def covers(self, minute_of_day: int) -> bool:
        if self.start_minute == self.end_minute:
            return False
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        # A window that crosses midnight, like 23:00–08:00.
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


def parse_quiet_hours(value: str) -> QuietHours | None:
    """"23:00-08:00" → окно тишины; пустая строка — тишины нет."""
    if not value.strip():
        return None
    try:
        start, end = value.split("-", 1)
        start_h, start_m = (int(part) for part in start.strip().split(":", 1))
        end_h, end_m = (int(part) for part in end.strip().split(":", 1))
    except ValueError:
        raise ValueError(f"quiet_hours must look like 23:00-08:00, got {value!r}") from None
    return QuietHours(start_h * 60 + start_m, end_h * 60 + end_m)


class TelegramNotifier:
    def __init__(
        self,
        sender: Any,
        settings: Any,
        horizon_h: Callable[[], str],
        now: Callable[[], datetime] = datetime.now,
        size_usd: Callable[[], Decimal] | None = None,
    ) -> None:
        self._sender = sender
        self._settings = settings
        self._horizon_h = horizon_h
        self._size_usd = size_usd
        self._now = now
        self._quiet = parse_quiet_hours(settings.quiet_hours)
        self._sent_at: deque[float] = deque()
        self._texts: dict[str, str] = {}
        self._best_total: Decimal | None = None
        self._best_day: str | None = None
        self.skipped_quiet = 0
        self.skipped_threshold = 0
        self.skipped_rate = 0

    # ── gaps ────────────────────────────────────────────────────────────────

    def on_alerts(self, alerts: list[GapAlert]) -> None:
        gate = self._gate()
        for alert in alerts:
            if not passes(alert, gate):
                self.skipped_threshold += 1
                continue
            moment = self._now()
            if self._quiet and self._quiet.covers(moment.hour * 60 + moment.minute):
                self.skipped_quiet += 1
                continue
            if not self._within_rate(moment):
                self.skipped_rate += 1
                continue
            message = gap(alert, int(moment.timestamp() * 1000), self._horizon_h(), self._is_best_of_day(alert, moment))
            self._texts[alert.key] = message.text
            self._sender.post(message, key=alert.key)

    def on_finished(self, finished: list[FinishedAlert]) -> None:
        if not self._settings.send_outcome:
            return
        for item in finished:
            sent = self._sender.message_ids.pop(item.key, None)
            text = self._texts.pop(item.key, None)
            if not sent or text is None:
                continue
            note = closed_note(item.lifetime_ms, item.peak_total_pct, "gone")
            self._sender.amend(sent, f"{text}\n\n{note}")

    # ── alarms and digest ───────────────────────────────────────────────────

    def on_event(self, event: JournalEvent) -> None:
        if not self._settings.alarms or event.type not in ALARM_TYPES:
            return
        if event.level is Level.INFO and event.type not in ("started", "stopped", "exchange_up", "db_connected"):
            return
        detail = ", ".join(
            str(value) for key, value in (("exchange", event.exchange), *sorted(event.payload.items())) if value not in (None, "")
        )
        self._sender.post(alarm(event.type, detail))

    def send_digest(self, numbers: DigestNumbers) -> None:
        self._sender.post(digest(numbers))

    # ── internals ───────────────────────────────────────────────────────────

    def _gate(self) -> PhoneGate:
        """The filters of the screen, in the terminal's own numbers: capacity defaults to the size being traded."""
        settings = self._settings
        capacity = settings.min_capacity_usd
        if capacity is None and self._size_usd is not None:
            capacity = self._size_usd()
        return PhoneGate(
            min_interest=settings.min_interest,
            min_total_pct=None if settings.min_total_pct is None else Decimal(str(settings.min_total_pct)),
            max_total_pct=None if settings.max_total_pct is None else Decimal(str(settings.max_total_pct)),
            min_volume24h_usd=None if settings.min_volume24h_usd is None else Decimal(str(settings.min_volume24h_usd)),
            min_capacity_usd=None if capacity is None else Decimal(str(capacity)),
        )

    def _within_rate(self, moment: datetime) -> bool:
        limit = self._settings.max_messages_per_hour
        now = moment.timestamp()
        while self._sent_at and now - self._sent_at[0] > 3600:
            self._sent_at.popleft()
        if limit and len(self._sent_at) >= limit:
            return False
        self._sent_at.append(now)
        return True

    def _is_best_of_day(self, alert: GapAlert, moment: datetime) -> bool:
        """True only when this gap beat one already announced today — the first gap of a day beats nothing."""
        day = moment.strftime("%Y-%m-%d")
        if day != self._best_day:
            self._best_day, self._best_total = day, None
        if alert.total_pct in (None, ""):
            return False
        total = Decimal(str(alert.total_pct))
        previous, self._best_total = self._best_total, total if self._best_total is None else max(total, self._best_total)
        return previous is not None and total > previous

    def stats(self) -> dict[str, Any]:
        return {
            "skipped_quiet": self.skipped_quiet,
            "skipped_threshold": self.skipped_threshold,
            "skipped_rate": self.skipped_rate,
            **self._sender.stats(),
        }
