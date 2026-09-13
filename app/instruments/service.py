"""
VFP: Keeps the current list of Binance–MEXC pairs with their units, steps, suspicion and manual flags, refreshed from public data.
Changes when: pairs gain new attributes, another exchange joins, or the refresh policy changes.
Anti-goal:
1. A half-refreshed catalog — if either exchange fails to load, the previous catalog stays in place.
2. Suspicious or blacklisted pairs looking tradable — tradable() is the single answer every consumer uses.
3. Needing API keys — everything here comes from public endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from app.config.settings import InstrumentsSettings
from app.core.links import trade_url
from app.core.pairs import PairAssessment, assess_pair, match_instruments
from app.core.schemas import Instrument
from app.journal.journal import Journal, Level
from app.storage.catalog import StoredPair, pair_key, save_catalog, set_pair_flags

log = logging.getLogger(__name__)

EXCHANGE_A = "binance"
EXCHANGE_B = "mexc"


class UnknownPair(KeyError):
    pass


@dataclass
class PairRecord:
    key: str
    assessment: PairAssessment
    pair_id: int | None = None
    manually_verified: bool = False
    blacklisted: bool = False

    @property
    def suspicious(self) -> bool:
        return self.assessment.suspicious_reason is not None and not self.manually_verified

    @property
    def tradable(self) -> bool:
        return not self.blacklisted and not self.suspicious


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _leg(instrument: Instrument, price_per_token: Decimal | None) -> dict[str, Any]:
    return {
        "exchange": instrument.exchange,
        "symbol": instrument.symbol_raw,
        "url": trade_url(instrument),
        "qty_unit_tokens": _text(instrument.qty_unit_tokens),
        "price_unit_tokens": _text(instrument.price_unit_tokens),
        "step_tokens": _text(instrument.qty_step_tokens),
        "min_qty_tokens": _text(instrument.min_qty_tokens),
        "min_notional_usd": _text(instrument.min_notional_usd),
        "price_per_token": _text(price_per_token),
    }


class InstrumentService:
    def __init__(
        self,
        adapters: Callable[[str], Any],
        database: Any,
        journal: Journal,
        settings: InstrumentsSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """adapters(name) returns the current adapter of an exchange; database is app.storage.database.Database."""
        self._adapters = adapters
        self._database = database
        self._journal = journal
        self._settings = settings
        self._clock = clock
        self._pairs: dict[str, PairRecord] = {}
        self._refreshed_at_ms: int | None = None
        self._refreshing = False
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("instrument refresh failed")
            await asyncio.sleep(self._settings.refresh_interval_s)

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            self._refreshing = True
            try:
                return await self._refresh()
            finally:
                self._refreshing = False

    async def _refresh(self) -> dict[str, Any]:
        adapter_a, adapter_b = self._adapters(EXCHANGE_A), self._adapters(EXCHANGE_B)
        try:
            instruments_a, instruments_b, quotes_a, quotes_b = await asyncio.gather(
                adapter_a.load_instruments(),
                adapter_b.load_instruments(),
                adapter_a.fetch_quotes(),
                adapter_b.fetch_quotes(),
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"[:300]
            self._journal.emit(Level.WARNING, "instruments", "refresh_failed", error=self._last_error)
            return self.summary()

        assessments = [
            assess_pair(
                a,
                b,
                quotes_a.get(a.symbol_raw),
                quotes_b.get(b.symbol_raw),
                self._settings.max_price_gap_pct,
                self._settings.max_index_gap_pct,
            )
            for a, b in match_instruments(instruments_a, instruments_b)
        ]

        stored: dict[str, StoredPair] = {}
        if self._database.ready:
            try:
                stored = await save_catalog(self._database.pool, instruments_a + instruments_b, assessments)
            except Exception:
                log.exception("saving the catalog failed")
        previous = self._pairs
        pairs: dict[str, PairRecord] = {}
        for assessment in assessments:
            key = pair_key(assessment)
            flags = stored.get(key)
            old = previous.get(key)
            pairs[key] = PairRecord(
                key=key,
                assessment=assessment,
                pair_id=flags.pair_id if flags else (old.pair_id if old else None),
                manually_verified=flags.manually_verified if flags else (old.manually_verified if old else False),
                blacklisted=flags.blacklisted if flags else (old.blacklisted if old else False),
            )

        self._pairs = pairs
        self._refreshed_at_ms = int(self._clock() * 1000)
        self._last_error = None
        summary = self.summary()
        self._journal.emit(
            Level.INFO,
            "instruments",
            "refreshed",
            binance=len(instruments_a),
            mexc=len(instruments_b),
            pairs=summary["pairs"],
            suspicious=summary["suspicious"],
            saved=bool(stored),
        )
        return summary

    async def set_flags(self, key: str, manually_verified: bool | None, blacklisted: bool | None) -> dict[str, Any]:
        record = self._pairs.get(key)
        if record is None:
            raise UnknownPair(key)
        if record.pair_id is not None and self._database.ready:
            stored = await set_pair_flags(self._database.pool, record.pair_id, manually_verified, blacklisted)
            record.manually_verified, record.blacklisted = stored.manually_verified, stored.blacklisted
        else:
            # Without the database the decision lives until the next restart only.
            if manually_verified is not None:
                record.manually_verified = manually_verified
            if blacklisted is not None:
                record.blacklisted = blacklisted
        self._journal.emit(
            Level.INFO,
            "instruments",
            "pair_flags",
            token=record.assessment.token,
            pair=key,
            manually_verified=record.manually_verified,
            blacklisted=record.blacklisted,
        )
        return self.describe_one(record)

    def tradable(self) -> list[PairRecord]:
        return [record for record in self._pairs.values() if record.tradable]

    def records(self) -> list[PairRecord]:
        return list(self._pairs.values())

    def summary(self) -> dict[str, Any]:
        records = self._pairs.values()
        return {
            "pairs": len(self._pairs),
            "tradable": sum(1 for record in records if record.tradable),
            "suspicious": sum(1 for record in records if record.suspicious),
            "blacklisted": sum(1 for record in records if record.blacklisted),
            "refreshed_at_ms": self._refreshed_at_ms,
            "refreshing": self._refreshing,
            "error": self._last_error,
        }

    @staticmethod
    def describe_one(record: PairRecord) -> dict[str, Any]:
        assessment = record.assessment
        return {
            "key": record.key,
            "pair_id": record.pair_id,
            "token": assessment.token,
            "a": _leg(assessment.a, assessment.price_a_per_token),
            "b": _leg(assessment.b, assessment.price_b_per_token),
            "common_step_tokens": _text(assessment.common_step_tokens),
            "min_qty_tokens": _text(assessment.min_qty_tokens),
            "price_gap_pct": _text(assessment.price_gap_pct),
            "index_gap_pct": _text(assessment.index_gap_pct),
            "volume24h_weak_usd": _text(assessment.volume24h_weak_usd),
            "reason": assessment.suspicious_reason,
            "manually_verified": record.manually_verified,
            "blacklisted": record.blacklisted,
            "suspicious": record.suspicious,
            "tradable": record.tradable,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "pairs": [self.describe_one(record) for record in self._pairs.values()],
        }
