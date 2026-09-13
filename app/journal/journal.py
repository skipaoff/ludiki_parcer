"""
VFP: One place every component reports a notable event to; the event fans out to storage and the interface without waiting.
Changes when: a new consumer of journal events appears or the event shape changes.
Anti-goal:
1. Blocking the caller — sinks must be non-blocking; a slow disk or browser never delays trading.
2. A failing sink hiding the event from the others — each sink is isolated.
3. Secrets in payloads — callers pass masked values only.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

log = logging.getLogger(__name__)


class Level(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class JournalEvent:
    ts: datetime
    level: Level
    source: str
    type: str
    exchange: str | None = None
    trade_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Row for the `events` table."""
        return {
            "ts": self.ts,
            "level": self.level.value,
            "source": self.source,
            "type": self.type,
            "exchange": self.exchange,
            "trade_id": self.trade_id,
            "payload": self.payload or None,
        }

    def to_wire(self) -> dict[str, Any]:
        """Shape sent to the browser."""
        return {
            "ts_ms": int(self.ts.timestamp() * 1000),
            "level": self.level.value,
            "source": self.source,
            "type": self.type,
            "exchange": self.exchange,
            "trade_id": self.trade_id,
            "payload": self.payload,
        }


Sink = Callable[[JournalEvent], None]


class Journal:
    def __init__(self, buffer_size: int = 500, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._recent: deque[JournalEvent] = deque(maxlen=buffer_size)
        self._sinks: list[Sink] = []
        self._clock = clock

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def emit(
        self,
        level: Level,
        source: str,
        type: str,
        *,
        exchange: str | None = None,
        trade_id: int | None = None,
        **payload: Any,
    ) -> JournalEvent:
        event = JournalEvent(self._clock(), level, source, type, exchange, trade_id, payload)
        self._recent.append(event)
        log.log(_LOG_LEVELS[level], "%s.%s %s", source, type, payload or "")
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:
                log.exception("journal sink failed for %s.%s", source, type)
        return event

    def recent(self, limit: int) -> list[JournalEvent]:
        """Newest last, at most limit events."""
        if limit <= 0:
            return []
        return list(self._recent)[-limit:]


_LOG_LEVELS = {Level.INFO: logging.INFO, Level.WARNING: logging.WARNING, Level.CRITICAL: logging.CRITICAL}
