"""
VFP: Row ids issued by the terminal itself, so rows that reference each other can be queued and spooled before the database sees them.
Changes when: the id layout or the tables that use local ids change.
Anti-goal:
1. Asking the database for ids on the hot path — an episode must get its id even while the database is down.
2. Collisions with sequence-issued ids — local ids start far above anything a BIGSERIAL reaches in practice.

Layout: milliseconds since the Unix epoch shifted left by 12 bits, plus a per-millisecond counter (4096 ids per ms).
"""

from __future__ import annotations

import time
from typing import Callable

COUNTER_BITS = 12
COUNTER_MASK = (1 << COUNTER_BITS) - 1


class LocalIds:
    def __init__(self, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self._clock_ms = clock_ms
        self._last_ms = 0
        self._counter = 0

    def next(self) -> int:
        now = max(self._clock_ms(), self._last_ms)
        if now == self._last_ms:
            self._counter += 1
            if self._counter > COUNTER_MASK:
                now += 1
                self._counter = 0
        else:
            self._counter = 0
        self._last_ms = now
        return (now << COUNTER_BITS) | self._counter
