"""
VFP: The feed settings a user may change while the terminal runs — validation, persistence with history, and applying them to the engine.
Changes when: another feed setting becomes editable from the interface.
Anti-goal:
1. Accepting nonsense silently — out-of-range values are refused with a reason.
2. Losing a change when the database is down — the value applies immediately; history is written when possible.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config.settings import FeedSettings
from app.journal.journal import Journal, Level
from app.storage.settings_store import load_latest, record_change
from app.strategies.price_gap.engine import PriceGapEngine

log = logging.getLogger(__name__)

PREFIX = "feed."
EDITABLE = {
    "size_usd": (Decimal("5"), Decimal("1000000")),
    "min_roi_pct": (Decimal("-5"), Decimal("50")),
    "enter_after_ms": (Decimal("0"), Decimal("3600000")),
    "funding_horizon_h": (Decimal("1"), Decimal("168")),
}
WHOLE_NUMBERS = frozenset({"enter_after_ms"})


class InvalidSetting(ValueError):
    pass


def parse_changes(body: dict[str, Any]) -> dict[str, Decimal | int]:
    changes: dict[str, Decimal | int] = {}
    for name, value in body.items():
        if name not in EDITABLE:
            raise InvalidSetting(f"{name} cannot be changed here")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise InvalidSetting(f"{name} must be a number") from None
        low, high = EDITABLE[name]
        if not number.is_finite() or not low <= number <= high:
            raise InvalidSetting(f"{name} must be between {low} and {high}")
        if name in WHOLE_NUMBERS:
            if number != number.to_integral_value():
                raise InvalidSetting(f"{name} must be a whole number")
            changes[name] = int(number)
        else:
            changes[name] = number
    return changes


class FeedSettingsService:
    def __init__(self, engine: PriceGapEngine, database: Any, journal: Journal) -> None:
        self._engine = engine
        self._database = database
        self._journal = journal

    def current(self) -> dict[str, str]:
        settings = self._engine.settings()
        return {name: str(getattr(settings, name)) for name in EDITABLE}

    async def load(self) -> None:
        if not self._database.ready:
            return
        try:
            stored = await load_latest(self._database.pool, PREFIX)
            changes = parse_changes({key.removeprefix(PREFIX): value for key, value in stored.items() if key.removeprefix(PREFIX) in EDITABLE})
        except Exception as exc:
            log.warning("stored feed settings ignored: %s", exc)
            return
        if changes:
            self._engine.apply_settings(self._engine.settings().model_copy(update=changes))

    async def update(self, body: dict[str, Any]) -> dict[str, str]:
        changes = parse_changes(body)
        before = self.current()
        self._engine.apply_settings(self._engine.settings().model_copy(update=changes))
        after = self.current()
        for name in changes:
            if before[name] == after[name]:
                continue
            if self._database.ready:
                try:
                    await record_change(self._database.pool, PREFIX + name, before[name], after[name])
                except Exception as exc:
                    log.warning("settings history write failed: %s", exc)
            self._journal.emit(Level.INFO, "settings", "changed", key=PREFIX + name, old=before[name], new=after[name])
        return after
