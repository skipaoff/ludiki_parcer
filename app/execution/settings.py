"""
VFP: The trading settings a user may change while the terminal runs — validation, persistence with history, and applying them to execution.
Changes when: another trading setting becomes editable from the interface.
Anti-goal:
1. The master switch among them: `[trading] enabled` stays a file setting, so nothing on screen can start live trading.
2. Losing a change when the database is down — the value applies immediately; history is written when possible.
"""

from __future__ import annotations

import logging
from typing import Any

from app.journal.journal import Journal, Level
from app.storage.settings_store import load_latest, record_change

log = logging.getLogger(__name__)

PREFIX = "trading."
EDITABLE = ("fast_trading",)


class InvalidSetting(ValueError):
    pass


def parse_changes(body: dict[str, Any]) -> dict[str, bool]:
    changes: dict[str, bool] = {}
    for name, value in body.items():
        if name not in EDITABLE:
            raise InvalidSetting(f"{name} cannot be changed here")
        if isinstance(value, bool):
            changes[name] = value
        elif isinstance(value, str) and value.lower() in ("true", "false"):
            changes[name] = value.lower() == "true"
        else:
            raise InvalidSetting(f"{name} must be true or false")
    return changes


class TradingSettingsService:
    def __init__(self, execution: Any, database: Any, journal: Journal) -> None:
        self._execution = execution
        self._database = database
        self._journal = journal

    def current(self) -> dict[str, str]:
        settings = self._execution.settings()
        return {name: str(getattr(settings, name)) for name in EDITABLE}

    async def load(self) -> None:
        if not self._database.ready:
            return
        try:
            stored = await load_latest(self._database.pool, PREFIX)
            changes = parse_changes({key.removeprefix(PREFIX): value for key, value in stored.items() if key.removeprefix(PREFIX) in EDITABLE})
        except Exception as exc:
            log.warning("stored trading settings ignored: %s", exc)
            return
        if changes:
            self._execution.apply_settings(self._execution.settings().model_copy(update=changes))

    async def update(self, body: dict[str, Any]) -> dict[str, str]:
        changes = parse_changes(body)
        before = self.current()
        self._execution.apply_settings(self._execution.settings().model_copy(update=changes))
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
