"""
VFP: Keeps the current list of cross-exchange pairs (every two enabled exchanges) with their units, steps, suspicion and manual flags, refreshed from public data.
Changes when: pairs gain new attributes or the refresh policy changes.
Anti-goal:
1. Pairs vanishing because one exchange failed to answer — its last good load stands in until it recovers.
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
from app.core.pairs import PairAssessment, Quote, assess_pair, match_all, rename_lonely_contracts
from app.core.schemas import Instrument
from app.journal.journal import Journal, Level
from app.storage.catalog import StoredPair, pair_key, save_catalog, set_pair_flags

log = logging.getLogger(__name__)

RETRY_AFTER_FAILURE_S = 30  # an exchange that never loaded is retried after this, doubling on every miss...
RETRY_MISSING_MAX_S = 300  # ...up to this
RETRY_WITH_LAST_GOOD_S = 300


class UnknownPair(KeyError):
    pass


def assess_catalog(
    by_exchange: dict[str, list[Instrument]],
    quotes: dict[tuple[str, str], Quote],
    order: list[str],
    max_price_gap_pct: Decimal,
    max_index_gap_pct: Decimal,
) -> list[PairAssessment]:
    # A contract left without a pair may only be spelled differently; the index price says whether it is the
    # same asset, and if it is, it joins the group under the name the rest of the market uses.
    named = rename_lonely_contracts(by_exchange, _index_per_token(by_exchange, quotes))
    return [
        assess_pair(a, b, quotes.get((a.exchange, a.symbol_raw)), quotes.get((b.exchange, b.symbol_raw)), max_price_gap_pct, max_index_gap_pct)
        for a, b in match_all(named, order)
    ]


def _index_per_token(by_exchange: dict[str, list[Instrument]], quotes: dict[tuple[str, str], Quote]) -> dict[tuple[str, str], Decimal]:
    """Index price of one token on one exchange, in per-token units so multiplier contracts compare."""
    prices: dict[tuple[str, str], Decimal] = {}
    for exchange, instruments in by_exchange.items():
        for item in instruments:
            quote = quotes.get((exchange, item.symbol_raw))
            if quote is None:
                continue
            price = quote.index if quote.index and quote.index > 0 else quote.mark
            if price and price > 0:
                prices[(exchange, item.token)] = price / item.price_unit_tokens
    return prices


@dataclass
class PairRecord:
    key: str
    assessment: PairAssessment
    pair_id: int | None = None
    manually_verified: bool = False
    blacklisted: bool = False
    instrument_a_id: int | None = None
    instrument_b_id: int | None = None

    @property
    def suspicious(self) -> bool:
        return self.assessment.suspicious_reason is not None and not self.manually_verified

    @property
    def manual_only(self) -> bool:
        return self.assessment.manual_only

    @property
    def tradable(self) -> bool:
        # manual_only pairs stay tradable here: they earn order books and a place in the feed like any other,
        # and it is the Open button that refuses them. Hiding them would lose gaps that can be taken by hand.
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
        exchanges: list[str],
        adapters: Callable[[str], Any],
        database: Any,
        journal: Journal,
        settings: InstrumentsSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        exchanges — enabled exchange names in a fixed order (the earlier one is leg a of a pair);
        adapters(name) returns the current adapter of an exchange; database is app.storage.database.Database.
        """
        self._exchanges = list(exchanges)
        self._last_good: dict[str, tuple[list[Instrument], dict[str, Quote]]] = {}
        self._adapters = adapters
        self._database = database
        self._journal = journal
        self._settings = settings
        self._clock = clock
        self._pairs: dict[str, PairRecord] = {}
        # Bumps whenever the set of pair records is replaced, so readers can skip rebuilding what did not change.
        self.version = 0
        self._refreshed_at_ms: int | None = None
        self._refreshing = False
        self._failures: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def _last_error(self) -> str | None:
        return "; ".join(f"{name}: {error}" for name, error in self._failures.items())[:300] or None

    def missing(self) -> list[str]:
        """Enabled exchanges without a single good load yet."""
        return [name for name in self._exchanges if name not in self._last_good]

    async def run(self) -> None:
        full_due = 0.0
        misses = 0
        while True:
            try:
                if time.monotonic() >= full_due:
                    await self.refresh()
                    # An exchange that failed with a last good load standing in is retried less eagerly, since every full
                    # refresh re-assesses the whole catalog.
                    pause = RETRY_WITH_LAST_GOOD_S if self._failures else self._settings.refresh_interval_s
                    full_due = time.monotonic() + min(pause, self._settings.refresh_interval_s)
                elif self.missing():
                    await self.refresh(only=self.missing())
            except Exception:
                log.exception("instrument refresh failed")
            # An exchange that never loaded (a startup timeout is common, or a venue unreachable for the day) is retried
            # alone and ever less often; the catalog is rebuilt only when it finally answers.
            misses = misses + 1 if self.missing() else 0
            pause = full_due - time.monotonic()
            if misses:
                pause = min(pause, RETRY_AFTER_FAILURE_S * 2 ** min(misses - 1, 8), RETRY_MISSING_MAX_S)
            await asyncio.sleep(max(1.0, pause))

    async def refresh(self, only: list[str] | None = None) -> dict[str, Any]:
        """Loads every enabled exchange, or only the named ones with the rest taken from their last good load."""
        async with self._lock:
            self._refreshing = True
            try:
                return await self._refresh(only)
            finally:
                self._refreshing = False

    async def _load(self, name: str) -> tuple[list[Instrument], dict[str, Quote]]:
        adapter = self._adapters(name)
        instruments, quotes = await asyncio.gather(adapter.load_instruments(), adapter.fetch_quotes())
        return instruments, quotes

    async def _refresh(self, only: list[str] | None) -> dict[str, Any]:
        names = self._exchanges if only is None else [name for name in self._exchanges if name in only]
        results = await asyncio.gather(*(self._load(name) for name in names), return_exceptions=True)
        failures = {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                failures[name] = f"{type(result).__name__}: {result}"[:300]
                continue
            self._last_good[name] = result
            self._failures.pop(name, None)
        if failures:
            self._failures.update(failures)
            error = "; ".join(f"{name}: {error}" for name, error in failures.items())[:300]
            self._journal.emit(Level.WARNING, "instruments", "refresh_failed", error=error, exchanges=sorted(failures))
        if only is not None and len(failures) == len(names):
            return self.summary()  # nothing new arrived: the catalog stands as it is
        # One exchange failing must not drop its pairs: the last good load stands in until it recovers.
        loaded = {name: self._last_good[name] for name in self._exchanges if name in self._last_good}
        if len(loaded) < 2:
            return self.summary()

        by_exchange = {name: instruments for name, (instruments, _) in loaded.items()}
        quotes = {(name, symbol): quote for name, (_, exchange_quotes) in loaded.items() for symbol, quote in exchange_quotes.items()}
        # Ten exchanges make some 18,000 pairs and half a second of Decimal work: done off the event loop.
        assessments = await asyncio.to_thread(
            assess_catalog, by_exchange, quotes, self._exchanges, self._settings.max_price_gap_pct, self._settings.max_index_gap_pct
        )
        all_instruments = [instrument for instruments in by_exchange.values() for instrument in instruments]

        stored: dict[str, StoredPair] = {}
        if self._database.ready:
            try:
                stored = await save_catalog(self._database.pool, all_instruments, assessments)
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
                instrument_a_id=flags.instrument_a_id if flags else (old.instrument_a_id if old else None),
                instrument_b_id=flags.instrument_b_id if flags else (old.instrument_b_id if old else None),
            )

        self._pairs = pairs
        self.version += 1
        self._refreshed_at_ms = int(self._clock() * 1000)
        summary = self.summary()
        self._journal.emit(
            Level.INFO,
            "instruments",
            "refreshed",
            contracts={name: len(instruments) for name, instruments in by_exchange.items()},
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
