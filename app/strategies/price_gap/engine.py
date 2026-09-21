"""
VFP: Every tick, turns live market data into the gap feed — best-price radar over all pairs, order books for candidates, book-based ROI, capacity, exit spread, funding over the horizon, expected result and interest, and the lifecycle of each gap.
Changes when: how gaps are found, measured or presented changes (PLAN.md, sections 5, 7 and 8).
Anti-goal:
1. ROI from best prices in the feed — only book-based ROI on the configured size is shown as ROI.
2. Stale data extending a gap — a leg older than the freshness limit counts as no measurement.
3. Trading decisions or orders — the engine measures; execution acts.
4. Awaiting anything inside a tick — the tick is plain computation over memory.
5. Knowing which exchanges exist — pairs and feeds arrive from the catalog and the composition root.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from operator import attrgetter
from typing import Any, Callable, Mapping, Protocol

from app.config.settings import FeedSettings
from app.core.episodes import EpisodeRules, EpisodeState, EventKind, Phase, Sample, end, step
from app.core.funding import FundingRate, PairFunding, pair_funding
from app.core.interest import interest
from app.core.links import trade_url
from app.core.qty import QtyRejected, plan_quantity
from app.core.radar import TopCheck, TopRoi, best_price_roi, choose_books, leg_fresh
from app.core.roi import EntryQuote, ExitQuote, best_entry, capacity_tokens, exit_quote
from app.core.schemas import Book, Fees, Instrument
from app.instruments.service import PairRecord
from app.market.state import MarketState
from app.strategies.price_gap.recorder import EpisodeSink, NullSink

log = logging.getLogger(__name__)

MIN_HOLD_MS = 10_000
CHOOSE_EVERY_MS = 1_000
CAPACITY_EVERY_MS = 1_000
TOP_STALE_MS = 60_000  # radar only ranks candidates; quiet contracts are re-seeded from REST every 30 s
RESUBSCRIBE_COOLDOWN_MS = 5_000
RADAR_SLICES = 5  # a large catalog's radar is refreshed over this many ticks...
RADAR_MAX_PAIRS_PER_TICK = 1500  # ...but never more pairs than this in one tick (18,000 pairs: every 2.4 s)
RADAR_FULL_PASS_PAIRS = 500  # up to this many pairs the whole radar is refreshed every tick
RADAR_PAIRS_PER_TOKEN = 10  # the radar lists coins; each coin brings at most this many of its pairs
_net_roi = attrgetter("roi_net_pct")


class Catalog(Protocol):
    def records(self) -> list[PairRecord]: ...


class DepthFeed(Protocol):
    def set_depth(self, symbols: Any) -> None: ...

    def resubscribe_depth(self, symbol: str) -> None: ...


@dataclass
class _Quote:
    """The latest book-based measurement of one pair."""

    ts_ms: int
    long: Instrument | None = None
    short: Instrument | None = None
    long_avg: Decimal | None = None
    short_avg: Decimal | None = None
    long_exit_avg: Decimal | None = None
    short_exit_avg: Decimal | None = None
    qty_tokens: Decimal | None = None
    roi_gross_pct: Decimal | None = None
    roi_net_pct: Decimal | None = None
    exit_spread_pct: Decimal | None = None
    age_long_ms: float | None = None
    age_short_ms: float | None = None
    problem: str | None = None
    capacity_usd: Decimal | None = None
    capacity_ts_ms: int = 0


@dataclass
class _BookNumbers:
    """
    Size, entry, exit and capacity of one pair computed from exact book objects. MarketState replaces a book on every
    update, so the same objects mean the same numbers — a quiet pair is not walked again every tick.
    """

    book_a: Book
    book_b: Book
    fees: tuple[Fees, Fees]
    settings: FeedSettings
    assessment: Any
    problem: str | None = None
    long: Instrument | None = None
    short: Instrument | None = None
    qty_tokens: Decimal | None = None
    entry: EntryQuote | None = None
    exit: ExitQuote | None = None
    capacity_usd: Decimal | None = None


def _text(value: Decimal | None, digits: int = 8) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    rounded = round(value, max(0, digits - value.adjusted() - 1))
    return format(rounded.normalize(), "f")


def _rate_view(rate: FundingRate | None) -> dict[str, Any] | None:
    if rate is None:
        return None
    # next_ms is per exchange: the feed shows both settlement timers, because the legs rarely settle together.
    return {"rate_pct": _text(rate.rate_pct, 4), "interval_h": _text(rate.interval_hours, 3), "next_ms": rate.next_ms}


def _worth_watching(record: PairRecord) -> bool:
    """
    Blacklisted pairs, and pairs whose per-token prices differ by more than the mismatch limit (a different asset under
    the same ticker, or other price units), would only fill the radar with fake gaps of hundreds of percent. A manual
    "verified" mark brings a pair back.
    """
    if record.blacklisted:
        return False
    return record.manually_verified or record.assessment.suspicious_reason != "price_mismatch"


class PriceGapEngine:
    def __init__(
        self,
        catalog: Catalog,
        state: MarketState,
        feeds: Mapping[str, DepthFeed],
        settings: FeedSettings,
        taker_fee_pct: Callable[[str], Decimal],
        on_catalog: Callable[[list[Instrument]], None],
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
        sink: EpisodeSink | None = None,
        pinned: Callable[[], list[str]] = lambda: [],
        fresh_ms_overrides: Mapping[str, int] | None = None,
        funding: Callable[[str, str], FundingRate | None] = lambda exchange, symbol: None,
        top_limit_ms_overrides: Mapping[str, int] | None = None,
    ) -> None:
        """
        feeds maps an exchange name to its order-book subscriptions; on_catalog receives every catalog contract;
        fresh_ms_overrides gives venues with slower quotes their own freshness limit (their quotes need no confirmation);
        funding(exchange, raw symbol) gives the current funding rate of a contract, None when unknown;
        top_limit_ms_overrides gives venues with slow quotes a shorter age limit for best prices in the radar.
        """
        self._funding = funding
        self._top_limits = dict(top_limit_ms_overrides or {})
        self._fresh_overrides = dict(fresh_ms_overrides or {})
        self._sink: EpisodeSink = sink or NullSink()
        self._pinned = pinned
        self._catalog = catalog
        self._state = state
        self._feeds = dict(feeds)
        self._settings = settings
        self._taker_fee_pct = taker_fee_pct
        self._on_catalog = on_catalog
        self._clock_ms = clock_ms
        self._rules = EpisodeRules(
            min_roi_net_pct=settings.min_roi_pct,
            enter_after_ms=settings.enter_after_ms,
            exit_hysteresis_pct=settings.exit_hysteresis_pct,
            exit_after_ms=settings.exit_after_ms,
        )
        self._catalog_keys: frozenset[str] = frozenset()
        self._catalog_version: int | None = None
        self._records: dict[str, PairRecord] = {}
        # Per pair: (key, record, exchange a, market key a, exchange b, market key b) — built once per catalog version.
        self._legs: list[tuple[str, PairRecord, str, tuple[str, str], str, tuple[str, str]]] = []
        self._catalog_exchanges: frozenset[str] = frozenset()
        self._fee_pct: dict[str, Decimal] = {}
        self._token_of: dict[str, str] = {}
        self._keys_by_token: dict[str, list[str]] = {}
        self._radar_cursor = 0
        self._top_by_key: dict[str, TopRoi] = {}
        self._tops_stale = True
        self._books: dict[str, int] = {}
        self._last_choose_ms = 0.0
        self._quotes: dict[str, _Quote] = {}
        self._book_numbers: dict[str, _BookNumbers] = {}
        # Funding per direction (long leg, short leg), recomputed when a rate or the settlements in the horizon change.
        self._funding_views: dict[tuple[str, str, str, str], tuple[Any, ...]] = {}
        # Radar rows without a gap episode, reused while everything they show is unchanged.
        self._row_memo: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._episodes: dict[str, EpisodeState] = {}
        self._episode_pair: dict[str, str] = {}
        self._tops: list[TopRoi] = []
        self._resubscribed_at: dict[tuple[str, str], float] = {}
        self._view: dict[str, Any] = {"rows": [], "radar": [], "stats": {}}
        self._tick_ms = 0.0
        self._lag_ms = 0.0
        self._gaps_entered = 0

    # ── loop ─────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        interval = self._settings.tick_ms / 1000
        expected = time.monotonic()
        while True:
            started = time.monotonic()
            self._lag_ms = max(0.0, (started - expected) * 1000)
            try:
                self.tick()
            except Exception:
                log.exception("price gap tick failed")
            self._tick_ms = (time.monotonic() - started) * 1000
            expected = started + interval
            await asyncio.sleep(max(0.0, expected - time.monotonic()))

    def tick(self) -> None:
        now = self._clock_ms()
        self._sync_catalog()
        if not self._records:
            return
        # Fees are looked up once per exchange per tick, not per pair.
        self._fee_pct = {exchange: self._taker_fee_pct(exchange) for exchange in self._catalog_exchanges}
        self._radar(now)
        # Ranking the whole radar costs ~10 ms at 18,000 pairs, so it happens when books are chosen (once a second) and
        # after the catalog changed; the view between rankings reads current values in the last order.
        if self._tops_stale or now - self._last_choose_ms >= CHOOSE_EVERY_MS:
            self._tops = sorted(self._top_by_key.values(), key=_net_roi, reverse=True)
            self._tops_stale = False
            if (self._tops or self._pinned()) and now - self._last_choose_ms >= CHOOSE_EVERY_MS:
                self._choose(now)
                self._last_choose_ms = now
        for key in list(self._books):
            record = self._records.get(key)
            if record is not None:
                self._measure(record, now)
        self._end_unwatched(now)
        self._view = self._build_view(now)

    # ── steps ────────────────────────────────────────────────────────────────

    def _sync_catalog(self) -> None:
        version = getattr(self._catalog, "version", None)
        if version is not None and version == self._catalog_version:
            return  # the same records as last tick; flags change on the records themselves
        self._catalog_version = version
        records = self._catalog.records()
        keys = frozenset(record.key for record in records)
        self._records = {record.key: record for record in records}
        self._legs = [
            (
                record.key,
                record,
                record.assessment.a.exchange,
                (record.assessment.a.exchange, record.assessment.a.symbol_raw),
                record.assessment.b.exchange,
                (record.assessment.b.exchange, record.assessment.b.symbol_raw),
            )
            for record in records
        ]
        if keys == self._catalog_keys:
            return
        self._catalog_keys = keys
        self._catalog_exchanges = frozenset(exchange for legs in self._legs for exchange in (legs[2], legs[4]))
        self._funding_views = {}
        self._row_memo = {}
        self._token_of = {record.key: record.assessment.token for record in records}
        self._keys_by_token = {}
        for record in records:
            self._keys_by_token.setdefault(record.assessment.token, []).append(record.key)
        self._radar_cursor = 0
        self._top_by_key = {key: top for key, top in self._top_by_key.items() if key in keys}
        self._tops_stale = True
        unique = {(leg.exchange, leg.symbol_raw): leg for record in records for leg in (record.assessment.a, record.assessment.b)}
        instruments = list(unique.values())
        self._state.set_instruments(instruments)
        self._on_catalog(instruments)

    def _fee(self, exchange: str) -> Decimal:
        fee = self._fee_pct.get(exchange)
        return self._taker_fee_pct(exchange) if fee is None else fee

    def _fees(self, long: Instrument, short: Instrument) -> Fees:
        return Fees(taker_long_pct=self._fee(long.exchange), taker_short_pct=self._fee(short.exchange))

    def _top_limit_ms(self, exchange: str) -> int:
        override = self._top_limits.get(exchange)
        return TOP_STALE_MS if override is None else min(TOP_STALE_MS, override)

    def _radar(self, now: float) -> None:
        """
        Best-price ROI of every pair into _top_by_key. A large catalog is refreshed a slice per tick, so one tick never stalls the
        event loop for the whole catalog (6,800 pairs over six exchanges took 85 ms a pass); each pair is still refreshed
        within RADAR_SLICES ticks, and books are chosen only once a second anyway.
        """
        legs = self._legs
        if len(legs) <= RADAR_FULL_PASS_PAIRS:
            batch, self._radar_cursor = legs, 0
        else:
            size = min(-(-len(legs) // RADAR_SLICES), RADAR_MAX_PAIRS_PER_TICK)
            start = self._radar_cursor
            batch = legs[start : start + size]
            self._radar_cursor = start + size if start + size < len(legs) else 0
        # Fees and age limits are looked up once per exchange, not per pair.
        fee = {exchange: float(self._fee(exchange)) for exchange in self._catalog_exchanges}
        oldest = {exchange: now - self._top_limit_ms(exchange) for exchange in self._catalog_exchanges}
        market_tops = self._state.tops
        ranked = self._top_by_key
        for key, record, exchange_a, market_a, exchange_b, market_b in batch:
            roi = None
            if _worth_watching(record):
                top_a = market_tops.get(market_a)
                top_b = market_tops.get(market_b)
                if top_a is not None and top_b is not None and top_a.received_ms >= oldest[exchange_a] and top_b.received_ms >= oldest[exchange_b]:
                    round_trip = (fee[exchange_a] + fee[exchange_b]) * 2  # entry and exit on both legs, as Fees.round_trip_pct
                    roi = best_price_roi(key, exchange_a, top_a.bid, top_a.ask, exchange_b, top_b.bid, top_b.ask, round_trip)
            if roi is None:
                ranked.pop(key, None)
            else:
                ranked[key] = roi
        return None

    def _choose(self, now: float) -> None:
        by_peak = sorted(self._episodes.items(), key=lambda item: item[1].roi_peak or Decimal(0), reverse=True)
        in_feed = [self._episode_pair[key] for key, episode in by_peak if episode.phase is Phase.IN_FEED]
        # A gap that is still earning its minimum lifetime must keep its books, or the clock could never run out.
        candidates_alive = [
            self._episode_pair[key]
            for key, episode in sorted(self._episodes.items(), key=lambda item: item[1].detected_ms)
            if episode.phase is Phase.CANDIDATE
        ]
        # Gaps that left the feed are followed until convergence, but never crowd out candidates entirely.
        tracking = [self._episode_pair[key] for key, episode in by_peak if episode.phase is Phase.TRACKING][
            : self._settings.tracking_limit
        ]
        # Open pairs come first: their exit spread and PnL depend on these books.
        watched = [key for key in self._pinned() if key in self._records] + in_feed + candidates_alive + tracking
        threshold = float(self._settings.min_roi_pct - self._settings.candidate_margin_pct)
        # Tradable pairs get order books first; pairs flagged suspicious only take slots that are left over. The tops are
        # ranked best-first, so the walk stops once they fall below the threshold and the radar has its tradable pairs.
        limit = self._settings.radar_rows
        tradable_candidates: list[str] = []
        other_candidates: list[str] = []
        tradable_radar: list[str] = []
        other_radar: list[str] = []
        for top in self._tops:
            above = top.roi_net_pct >= threshold
            if not above and len(tradable_radar) >= limit:
                break
            if self._records[top.key].tradable:
                if above:
                    tradable_candidates.append(top.key)
                if len(tradable_radar) < limit:
                    tradable_radar.append(top.key)
            else:
                if above:
                    other_candidates.append(top.key)
                if len(other_radar) < limit:
                    other_radar.append(top.key)
        radar = (tradable_radar + other_radar)[:limit]
        chosen = choose_books(
            watched,
            tradable_candidates + other_candidates + radar,
            self._books,
            int(now),
            self._settings.book_limit,
            MIN_HOLD_MS,
        )
        for key in set(self._books) - set(chosen):
            record = self._records.get(key)
            self._quotes.pop(key, None)
            self._book_numbers.pop(key, None)
            if record is not None:
                self._state.drop_book((record.assessment.a.exchange, record.assessment.a.symbol_raw))
                self._state.drop_book((record.assessment.b.exchange, record.assessment.b.symbol_raw))
        self._books = chosen
        for exchange, feed in self._feeds.items():
            symbols = []
            for key in chosen:
                record = self._records.get(key)
                if record is None:
                    continue
                for leg in (record.assessment.a, record.assessment.b):
                    if leg.exchange == exchange:
                        symbols.append(leg.symbol_raw)
            feed.set_depth(symbols)

    def _measure(self, record: PairRecord, now: float) -> None:
        a, b = record.assessment.a, record.assessment.b
        key_a, key_b = (a.exchange, a.symbol_raw), (b.exchange, b.symbol_raw)
        book_a, book_b = self._state.books.get(key_a), self._state.books.get(key_b)
        age_a, age_b = self._state.book_age_ms(key_a), self._state.book_age_ms(key_b)
        previous = self._quotes.get(record.key)
        quote = _Quote(ts_ms=int(now), age_long_ms=age_a, age_short_ms=age_b)
        if previous is not None:
            quote.capacity_usd, quote.capacity_ts_ms = previous.capacity_usd, previous.capacity_ts_ms

        subscribed_since = self._books[record.key]
        fresh = {}
        for leg_key, age, book in ((key_a, age_a, book_a), (key_b, age_b, book_b)):
            feed = self._feeds[leg_key[0]]
            override = self._fresh_overrides.get(leg_key[0])
            if override is not None:
                fresh[leg_key] = age is not None and age <= override
                continue
            top = self._state.tops.get(leg_key)
            fresh[leg_key] = leg_fresh(
                age,
                float(book.bids[0].price) if book and book.bids else None,
                float(book.asks[0].price) if book and book.asks else None,
                TopCheck(top.bid, top.ask) if top else None,
                self._settings.fresh_ms,
                self._settings.quiet_book_max_ms,
            )
            lost = (age is None and now - subscribed_since > self._settings.resubscribe_after_ms) or (
                age is not None and age > self._settings.quiet_book_max_ms
            )
            if lost and now - self._resubscribed_at.get(leg_key, 0) > RESUBSCRIBE_COOLDOWN_MS:
                self._resubscribed_at[leg_key] = now
                feed.resubscribe_depth(leg_key[1])

        sample_roi = None
        exit_spread = None
        if book_a is None or book_b is None or not book_a.asks or not book_b.asks:
            quote.problem = "no_book"
            self._book_numbers.pop(record.key, None)
        else:
            numbers = self._numbers(record, book_a, book_b)
            entry, exit = numbers.entry, numbers.exit
            if entry is None:
                quote.problem = numbers.problem
            else:
                long, short = numbers.long, numbers.short
                quote.long, quote.short = long, short
                quote.long_avg, quote.short_avg = entry.long_avg, entry.short_avg
                quote.qty_tokens = numbers.qty_tokens
                quote.roi_gross_pct, quote.roi_net_pct = entry.roi_gross_pct, entry.roi_net_pct
                quote.exit_spread_pct = exit.exit_spread_pct if exit else None
                quote.long_exit_avg = exit.long_exit_avg if exit else None
                quote.short_exit_avg = exit.short_exit_avg if exit else None
                quote.age_long_ms = age_a if long is a else age_b
                quote.age_short_ms = age_b if long is a else age_a
                if not (fresh[key_a] and fresh[key_b]):
                    quote.problem = "stale"
                else:
                    sample_roi, exit_spread = entry.roi_net_pct, quote.exit_spread_pct
                # Capacity is refreshed at most once a second, and never twice for the same books.
                if (
                    numbers.capacity_usd is None
                    and entry.roi_net_pct >= self._settings.min_roi_pct - self._settings.candidate_margin_pct
                    and now - quote.capacity_ts_ms >= CAPACITY_EVERY_MS
                ):
                    long_book, short_book = (book_a, book_b) if long is a else (book_b, book_a)
                    tokens = capacity_tokens(
                        long_book.asks,
                        short_book.bids,
                        numbers.fees[0] if long is a else numbers.fees[1],
                        self._settings.min_roi_pct,
                        record.assessment.common_step_tokens,
                    )
                    numbers.capacity_usd = tokens * entry.long_avg
                    quote.capacity_usd = numbers.capacity_usd
                    quote.capacity_ts_ms = int(now)
        self._quotes[record.key] = quote

        direction = quote.long.exchange if quote.long else None
        for episode_key in [k for k, pair in self._episode_pair.items() if pair == record.key]:
            if direction is None or not episode_key.endswith(f">{direction}"):
                # The other direction (or no measurement) sees no gap in this sample.
                self._advance(episode_key, record, Sample(int(now), None, None), None)
        if direction is not None:
            self._advance(f"{record.key}>{direction}", record, Sample(int(now), sample_roi, exit_spread), quote)

    def _numbers(self, record: PairRecord, book_a: Book, book_b: Book) -> _BookNumbers:
        a, b = record.assessment.a, record.assessment.b
        fees = (self._fees(a, b), self._fees(b, a))
        cached = self._book_numbers.get(record.key)
        if (
            cached is not None
            and cached.book_a is book_a
            and cached.book_b is book_b
            and cached.settings is self._settings
            and cached.assessment is record.assessment
            and cached.fees == fees
        ):
            return cached
        numbers = _BookNumbers(book_a, book_b, fees, self._settings, record.assessment)
        price = min(book_a.asks[0].price, book_b.asks[0].price)
        plan = plan_quantity(self._settings.size_usd, price, long=a, short=b, step=record.assessment.common_step_tokens)
        if isinstance(plan, QtyRejected):
            numbers.problem = plan.reason
        else:
            entry = best_entry(book_a, book_b, plan.qty_tokens, fees[0], fees[1])
            if entry is None:
                numbers.problem = "book_too_thin"
            else:
                long, short = (a, b) if entry.long_exchange == a.exchange else (b, a)
                long_book, short_book = (book_a, book_b) if long is a else (book_b, book_a)
                numbers.long, numbers.short, numbers.qty_tokens, numbers.entry = long, short, plan.qty_tokens, entry
                numbers.exit = exit_quote(long_book, short_book, plan.qty_tokens)
        self._book_numbers[record.key] = numbers
        return numbers

    def _advance(self, episode_key: str, record: PairRecord, sample: Sample, quote: _Quote | None) -> None:
        state, events = step(self._episodes.get(episode_key), sample, self._rules)
        for event in events:
            if event.kind is EventKind.ENTERED_FEED:
                if state.first_entered_feed_ms == event.ts_ms:
                    self._gaps_entered += 1
                self._sink.entered(episode_key, record, state, event.ts_ms)
            elif event.kind is EventKind.LEFT_FEED:
                self._sink.left_feed(episode_key, event.ts_ms)
            elif event.kind is EventKind.ENDED:
                self._sink.ended(episode_key, record, state, event.ts_ms)
        if state is None or state.phase is Phase.ENDED:
            self._episodes.pop(episode_key, None)
            self._episode_pair.pop(episode_key, None)
            return
        self._episodes[episode_key] = state
        self._episode_pair[episode_key] = record.key
        if state.phase in (Phase.IN_FEED, Phase.TRACKING):
            self._sink.sampled(episode_key, record, state, quote, sample.ts_ms)

    def _end_unwatched(self, now: float) -> None:
        for episode_key, pair_key in list(self._episode_pair.items()):
            if pair_key not in self._books:
                self._finish(episode_key, int(now), "evicted")

    def _finish(self, episode_key: str, now_ms: int, reason: str) -> None:
        state = self._episodes.pop(episode_key)
        pair_key = self._episode_pair.pop(episode_key)
        ended, _ = end(state, now_ms, reason)
        record = self._records.get(pair_key)
        if record is not None:
            self._sink.ended(episode_key, record, ended, now_ms)

    def close(self) -> None:
        """End every tracked gap with app_stop so its history row is complete; call before the write queue closes."""
        now = int(self._clock_ms())
        for episode_key in list(self._episodes):
            self._finish(episode_key, now, "app_stop")

    # ── view ─────────────────────────────────────────────────────────────────

    def _row(self, record: PairRecord, quote: _Quote | None, top: TopRoi | None, now: float, episode: EpisodeState | None) -> dict[str, Any]:
        assessment = record.assessment
        long = quote.long if quote and quote.long else None
        short = quote.short if quote and quote.short else None
        if long is None and top is not None:
            long, short = (assessment.a, assessment.b) if top.long_exchange == assessment.a.exchange else (assessment.b, assessment.a)
        funding, funding_view = (None, None) if long is None or short is None else self._pair_funding(long, short, now)
        memo_signature = None
        if episode is None:
            # Everything such a row shows; a radar pair without books keeps its row until its best prices are re-ranked.
            memo_signature = (
                record.assessment,
                record.blacklisted,
                record.suspicious,
                self._settings,
                funding_view,
                long,
                short,
                None if quote is None else (quote.qty_tokens, quote.roi_net_pct, quote.capacity_usd, quote.problem),
            )
            memo = self._row_memo.get(record.key)
            if memo is not None and memo[0] == memo_signature:
                return memo[1]
        block = None
        if record.blacklisted:
            block = "blacklisted"
        elif record.suspicious:
            block = "suspicious"
        elif quote is None:
            block = "no_book"
        elif quote.problem:
            block = quote.problem
        size = self._settings.size_usd
        lifetime_ms = int(now - episode.detected_ms) if episode else None
        profit_pct = quote.roi_net_pct if quote else None
        # Expected result: the spread after book and fees if prices converge, plus funding over the horizon.
        total_pct = None if profit_pct is None else profit_pct + (funding.horizon_pct if funding else Decimal(0))
        score = None
        if block != "stale":
            score = interest(
                total_pct,
                quote.capacity_usd if quote else None,
                size,
                lifetime_ms,
                assessment.volume24h_weak_usd,
                record.suspicious or record.blacklisted,
            )
        row = {
            "key": record.key,
            "token": assessment.token,
            "long": None if long is None else {"exchange": long.exchange, "symbol": long.symbol_raw, "url": trade_url(long)},
            "short": None if short is None else {"exchange": short.exchange, "symbol": short.symbol_raw, "url": trade_url(short)},
            "qty_tokens": _text(quote.qty_tokens, 12) if quote else None,
            # Profit in % of the size: book-based entry spread minus round-trip taker fees, if prices converge.
            "roi_net_pct": _text(quote.roi_net_pct, 4) if quote else None,
            "capacity_usd": _text(quote.capacity_usd, 6) if quote else None,
            "phase": episode.phase.value if episode else None,
            # Counted from the moment the gap appeared, so a feed row shows at least the required minimum lifetime.
            "lifetime_ms": lifetime_ms,
            "volume24h_weak_usd": _text(assessment.volume24h_weak_usd, 6),
            "suspicious": record.suspicious,
            "blacklisted": record.blacklisted,
            "block": block,
            "profit_usd": None if profit_pct is None else _text(profit_pct * size / 100, 4),
            "funding": funding_view,
            "total_pct": _text(total_pct, 4),
            "total_usd": None if total_pct is None else _text(total_pct * size / 100, 4),
            "funding_known": funding is not None,
            "score": None if score is None else score.score,
            "score_parts": None
            if score is None
            else {"result": score.result, "depth": score.depth, "stability": score.stability, "liquidity": score.liquidity},
        }
        if memo_signature is None:
            self._row_memo.pop(record.key, None)
        else:
            self._row_memo[record.key] = (memo_signature, row)
        return row

    def _pair_funding(self, long: Instrument, short: Instrument, now: float) -> tuple[PairFunding | None, dict[str, Any]]:
        """Funding of one direction, recomputed only when a rate changes or a settlement enters or leaves the horizon."""
        rate_long, rate_short = self._funding(long.exchange, long.symbol_raw), self._funding(short.exchange, short.symbol_raw)
        direction = (long.exchange, long.symbol_raw, short.exchange, short.symbol_raw)
        cached = self._funding_views.get(direction)
        if (
            cached is not None
            and (cached[0] is None or now < cached[0])
            and cached[1] is self._settings
            and cached[2] == rate_long
            and cached[3] == rate_short
        ):
            return cached[4], cached[5]
        size = self._settings.size_usd
        funding = pair_funding(rate_long, rate_short, int(now), self._settings.funding_horizon_h)
        view = {
            "long": _rate_view(rate_long),
            "short": _rate_view(rate_short),
            "hourly_pct": _text(funding.hourly_pct, 4) if funding else None,
            "horizon_pct": _text(funding.horizon_pct, 4) if funding else None,
            "horizon_usd": _text(funding.horizon_pct * size / 100, 4) if funding else None,
            "next_ms": funding.next_ms if funding else None,
            "next_pct": _text(funding.next_pct, 4) if funding and funding.next_pct is not None else None,
            "next_usd": _text(funding.next_pct * size / 100, 4) if funding and funding.next_pct is not None else None,
        }
        self._funding_views[direction] = (funding.changes_ms if funding else None, self._settings, rate_long, rate_short, funding, view)
        return funding, view

    def _is_current_direction(self, episode_key: str) -> bool:
        quote = self._quotes.get(self._episode_pair[episode_key])
        return quote is not None and quote.long is not None and episode_key.endswith(f">{quote.long.exchange}")

    def _build_view(self, now: float) -> dict[str, Any]:
        tops_by_key = self._top_by_key
        rows = []
        in_feed_pairs = set()
        # One row per pair: when both directions are in the feed, the one the book currently favours is shown.
        candidates = sorted(
            (
                (episode_key, episode)
                for episode_key, episode in self._episodes.items()
                if episode.phase is Phase.IN_FEED and self._episode_pair[episode_key] in self._records
            ),
            key=lambda item: not self._is_current_direction(item[0]),
        )
        for episode_key, episode in candidates:
            pair_key = self._episode_pair[episode_key]
            if pair_key in in_feed_pairs:
                continue
            in_feed_pairs.add(pair_key)
            record = self._records[pair_key]
            rows.append(self._row(record, self._quotes.get(pair_key), tops_by_key.get(pair_key), now, episode))
        rows.sort(key=lambda row: Decimal(row["roi_net_pct"] or "-1000"), reverse=True)

        # The radar lists coins: the best current spreads bring their coin in, with that coin's other pairs alongside.
        episodes_by_pair = {self._episode_pair[key]: state for key, state in self._episodes.items() if key in self._episode_pair}
        # Coins are picked from the ranked tops, stopping at radar_rows; their other pairs come from the token index, so
        # the view never walks the whole catalog (18,000 pairs over ten exchanges).
        # As with books, coins brought in by a tradable pair come first; a spread seen only on suspicious pairs (often a
        # different asset under the same ticker) takes a line only when slots are left.
        limit = self._settings.radar_rows
        coins: dict[str, None] = {}
        suspicious_coins: dict[str, None] = {}
        for top in self._tops:
            if len(coins) >= limit:
                break
            if top.key in in_feed_pairs:
                continue
            (coins if self._records[top.key].tradable else suspicious_coins)[self._token_of[top.key]] = None
        for token in suspicious_coins:
            if len(coins) >= limit:
                break
            coins.setdefault(token, None)
        radar = []
        for token in coins:
            ranked = sorted(
                (self._top_by_key[key] for key in self._keys_by_token[token] if key in self._top_by_key and key not in in_feed_pairs),
                key=_net_roi,
                reverse=True,
            )
            for top in ranked[:RADAR_PAIRS_PER_TOKEN]:
                radar.append(self._row(self._records[top.key], self._quotes.get(top.key), top, now, episodes_by_pair.get(top.key)))

        return {
            "rows": rows,
            "radar": radar,
            "settings": {
                "size_usd": _text(self._settings.size_usd),
                "min_roi_pct": _text(self._settings.min_roi_pct),
                "enter_after_ms": self._settings.enter_after_ms,
                "funding_horizon_h": _text(self._settings.funding_horizon_h),
                "fresh_ms": self._settings.fresh_ms,
                "taker_fee_pct": {exchange: _text(self._taker_fee_pct(exchange)) for exchange in self._feeds},
            },
            "stats": {
                "pairs": len(self._records),
                "radar_pairs": len(self._tops),
                "books": len(self._books),
                "tracked": len(self._episodes),
                "in_feed": len(rows),
                "gaps_entered": self._gaps_entered,
                "tick_ms": round(self._tick_ms, 1),
                "loop_lag_ms": round(self._lag_ms, 1),
            },
        }

    def view(self) -> dict[str, Any]:
        return self._view

    def episode_state(self, episode_key: str | None) -> EpisodeState | None:
        return None if episode_key is None else self._episodes.get(episode_key)

    def current_quote(self, pair_key: str) -> tuple[PairRecord, _Quote, str | None] | None:
        """The latest book measurement of a pair and the key of its live gap in that direction, if any."""
        record, quote = self._records.get(pair_key), self._quotes.get(pair_key)
        if record is None or quote is None or quote.long is None:
            return None
        episode_key = f"{pair_key}>{quote.long.exchange}"
        return record, quote, episode_key if episode_key in self._episodes else None

    @property
    def quote_max_age_ms(self) -> int:
        """A quote older than a few ticks is not a basis for sending orders."""
        return self._settings.tick_ms * 3

    def settings(self) -> FeedSettings:
        return self._settings

    def apply_settings(self, settings: FeedSettings) -> None:
        """Takes effect on the next tick; gaps already tracked are judged by the new threshold from now on."""
        self._settings = settings
        self._rules = EpisodeRules(
            min_roi_net_pct=settings.min_roi_pct,
            enter_after_ms=settings.enter_after_ms,
            exit_hysteresis_pct=settings.exit_hysteresis_pct,
            exit_after_ms=settings.exit_after_ms,
        )
        for quote in self._quotes.values():
            quote.capacity_ts_ms = 0
