"""
VFP: How long each pair has already stood in the feed over the last day, read from recorded history.
Changes when: the window or the source of the "this gap keeps standing" fact changes.
Anti-goal:
1. Counting it in memory only — the episode clock restarts with the terminal, and PENG came back to the top of
   the screen after every launch while the database knew it had stood there 200 minutes out of the last 24 hours.
2. Holding up the feed for the database — an unread history means zero, and the screen works as it always did.
"""

from __future__ import annotations

import asyncio
import logging

from app.storage.database import Database, is_connection_error
from app.storage.history import feed_seconds_by_pair

log = logging.getLogger(__name__)

WINDOW_HOURS = 24
REFRESH_S = 600
RETRY_S = 60


class StandingGaps:
    def __init__(self, database: Database, window_hours: int = WINDOW_HOURS) -> None:
        self._database = database
        self._window_hours = window_hours
        self._seconds: dict[int, float] = {}

    def seconds(self, pair_id: int | None) -> float:
        """Seconds this pair spent in the feed within the window; zero for anything unknown."""
        return 0.0 if pair_id is None else self._seconds.get(pair_id, 0.0)

    async def refresh(self) -> int:
        self._seconds = await feed_seconds_by_pair(self._database.pool, self._window_hours)
        return len(self._seconds)

    async def run(self) -> None:
        while True:
            delay = REFRESH_S
            try:
                if self._database.ready:
                    log.debug("standing gaps: %s pairs", await self.refresh())
                else:
                    delay = RETRY_S
            except Exception as error:  # noqa: BLE001 - a read that fails leaves the previous answer standing
                if not is_connection_error(error):
                    log.warning("standing gaps query failed: %s", error)
                delay = RETRY_S
            await asyncio.sleep(delay)
