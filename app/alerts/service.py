"""
VFP: Turns a gap that just became clickable into one journal event, which the browser shows as a notification.
Changes when: the cadence or the shape of a gap announcement changes.
Anti-goal:
1. Deciding here what deserves an alert — that is core/alerts.py, so it can be tested without a clock or a loop.
2. A second delivery channel: the journal already reaches the interface live and the events table for history.
3. Announcing gaps the moment the terminal starts, when every live gap looks new.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from app.core.alerts import AlertRules, decide
from app.journal.journal import Journal, Level

log = logging.getLogger(__name__)

INTERVAL_S = 1.0
QUIET_START_S = 20.0
"""The feed needs a moment to fill after a start; gaps found in that window are not news, they are the backlog."""


class GapAlerts:
    def __init__(
        self,
        rows: Callable[[], list[dict[str, Any]]],
        journal: Journal,
        rules: AlertRules | None = None,
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._rows = rows
        self._journal = journal
        self._rules = rules or AlertRules()
        self._clock_ms = clock_ms
        self._sleep = sleep
        self._announced: dict[str, int] = {}
        self.sent = 0

    async def run(self) -> None:
        await self._sleep(QUIET_START_S)
        self._catch_up()
        while True:
            await self._sleep(INTERVAL_S)
            try:
                self.check()
            except Exception as exc:  # a broken alert must not take the terminal down
                log.warning("gap alerts failed: %s", exc)

    def _catch_up(self) -> None:
        """Remember what is already on screen without announcing it."""
        now = int(self._clock_ms())
        _, self._announced = decide(self._rows(), self._announced, now, self._rules)

    def check(self) -> list[str]:
        now = int(self._clock_ms())
        alerts, self._announced = decide(self._rows(), self._announced, now, self._rules)
        for alert in alerts:
            self.sent += 1
            self._journal.emit(
                Level.INFO,
                "feed",
                "gap_actionable",
                key=alert.key,
                token=alert.token,
                long=alert.long_exchange,
                short=alert.short_exchange,
                total_pct=alert.total_pct,
                profit_pct=alert.profit_pct,
                interest=alert.interest,
                size_usd=alert.size_usd,
                blocks=list(alert.blocks),
            )
        return [alert.key for alert in alerts]
