"""
VFP: Every tick, turns live market data into the gap feed — best-price radar over all pairs, order books for candidates, book-based ROI, capacity, exit spread and the lifecycle of each gap.
Changes when: how gaps are found, measured or presented changes (PLAN.md, sections 5, 7 and 8).
Anti-goal:
1. ROI from best prices in the feed — only book-based ROI on the configured size is shown as ROI.
2. Stale data extending a gap — a leg older than the freshness limit counts as no measurement.
3. Trading decisions or orders — the feed is read-only until stage 6.
4. Awaiting anything inside a tick — the tick is plain computation over memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Protocol

from app.config.settings import FeedSettings
from app.core.episodes import EpisodeRules, EpisodeState, EventKind, Phase, Sample, end, step
from app.core.links import trade_url
from app.core.qty import QtyRejected, plan_quantity
from app.core.radar import TopRoi, best_price_roi, choose_books, leg_fresh
from app.core.roi import best_entry, capacity_tokens, exit_quote
from app.core.schemas import Fees, Instrument
from app.instruments.service import PairRecord
from app.market.state import MarketState
from app.strategies.price_gap.recorder import EpisodeSink, NullSink

log = logging.getLogger(__name__)

MIN_HOLD_MS = 10_000
CHOOSE_EVERY_MS = 1_000
CAPACITY_EVERY_MS = 1_000
TOP_STALE_MS = 60_000  # radar only ranks candidates; quiet contracts are re-seeded from REST every 30 s
RESUBSCRIBE_COOLDOWN_MS = 5_000


class Catalog(Protocol):
    def records(self) -> list[PairRecord]: ...


class DepthFeed(Protocol):
    def set_depth(self, symbols: Any) -> None: ...

    def resubscribe_depth(self, symbol: str) -> None: ...

    def stream_age_ms(self, symbol: str) -> float | None: ...


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


def _text(value: Decimal | None, digits: int = 8) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    rounded = round(value, max(0, digits - value.adjusted() - 1))
    return format(rounded.normalize(), "f")


def _ms(value: float | None) -> int | None:
    return None if value is None else int(value)


class PriceGapEngine:
    def __init__(
        self,
        catalog: Catalog,
        state: MarketState,
        binance: DepthFeed,
        mexc: DepthFeed,
        settings: FeedSettings,
        taker_fee_pct: Callable[[str], Decimal],
        on_radar_symbols: Callable[[list[str]], None],
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
        sink: EpisodeSink | None = None,
        pinned: Callable[[], list[str]] = lambda: [],
    ) -> None:
        self._sink: EpisodeSink = sink or NullSink()
        self._pinned = pinned
        self._catalog = catalog
        self._state = state
        self._feeds = {"binance": binance, "mexc": mexc}
        self._settings = settings
        self._taker_fee_pct = taker_fee_pct
        self._on_radar_symbols = on_radar_symbols
        self._clock_ms = clock_ms
        self._rules = EpisodeRules(
            min_roi_net_pct=settings.min_roi_pct,
            enter_after_ms=settings.enter_after_ms,
            exit_hysteresis_pct=settings.exit_hysteresis_pct,
            exit_after_ms=settings.exit_after_ms,
        )
        self._catalog_keys: frozenset[str] = frozenset()
        self._records: dict[str, PairRecord] = {}
        self._books: dict[str, int] = {}
        self._last_choose_ms = 0.0
        self._quotes: dict[str, _Quote] = {}
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
        self._tops = self._radar(now)
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
        records = self._catalog.records()
        keys = frozenset(record.key for record in records)
        if keys == self._catalog_keys:
            self._records = {record.key: record for record in records}
            return
        self._catalog_keys = keys
        self._records = {record.key: record for record in records}
        instruments = [leg for record in records for leg in (record.assessment.a, record.assessment.b)]
        self._state.set_instruments(instruments)
        self._on_radar_symbols([record.assessment.a.symbol_raw for record in records if record.assessment.a.exchange == "binance"])

    def _fees(self, long: Instrument, short: Instrument) -> Fees:
        return Fees(taker_long_pct=self._taker_fee_pct(long.exchange), taker_short_pct=self._taker_fee_pct(short.exchange))

    def _radar(self, now: float) -> list[TopRoi]:
        tops = []
        for key, record in self._records.items():
            a, b = record.assessment.a, record.assessment.b
            top_a = self._state.tops.get((a.exchange, a.symbol_raw))
            top_b = self._state.tops.get((b.exchange, b.symbol_raw))
            if top_a is None or top_b is None:
                continue
            if now - top_a.received_ms > TOP_STALE_MS or now - top_b.received_ms > TOP_STALE_MS:
                continue
            round_trip = float(self._fees(a, b).round_trip_pct)
            roi = best_price_roi(key, a.exchange, top_a.bid, top_a.ask, b.exchange, top_b.bid, top_b.ask, round_trip)
            if roi is not None:
                tops.append(roi)
        tops.sort(key=lambda top: top.roi_net_pct, reverse=True)
        return tops

    def _choose(self, now: float) -> None:
        by_peak = sorted(self._episodes.items(), key=lambda item: item[1].roi_peak or Decimal(0), reverse=True)
        in_feed = [self._episode_pair[key] for key, episode in by_peak if episode.phase is Phase.IN_FEED]
        # Gaps that left the feed are followed until convergence, but never crowd out candidates entirely.
        tracking = [self._episode_pair[key] for key, episode in by_peak if episode.phase is Phase.TRACKING][
            : self._settings.tracking_limit
        ]
        # Open pairs come first: their exit spread and PnL depend on these books.
        watched = [key for key in self._pinned() if key in self._records] + in_feed + tracking
        threshold = float(self._settings.min_roi_pct - self._settings.candidate_margin_pct)
        candidates = [top.key for top in self._tops if top.roi_net_pct >= threshold]
        radar = [top.key for top in self._tops[: self._settings.radar_rows]]
        chosen = choose_books(
            watched,
            candidates + radar,
            self._books,
            int(now),
            self._settings.book_limit,
            MIN_HOLD_MS,
        )
        for key in set(self._books) - set(chosen):
            record = self._records.get(key)
            self._quotes.pop(key, None)
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
        for leg_key, age in ((key_a, age_a), (key_b, age_b)):
            feed = self._feeds[leg_key[0]]
            fresh[leg_key] = leg_fresh(age, feed.stream_age_ms(leg_key[1]), self._settings.fresh_ms, self._settings.quiet_book_max_ms)
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
        else:
            price = min(book_a.asks[0].price, book_b.asks[0].price)
            plan = plan_quantity(self._settings.size_usd, price, long=a, short=b)
            if isinstance(plan, QtyRejected):
                quote.problem = plan.reason
            else:
                entry = best_entry(book_a, book_b, plan.qty_tokens, self._fees(a, b), self._fees(b, a))
                if entry is None:
                    quote.problem = "book_too_thin"
                else:
                    long, short = (a, b) if entry.long_exchange == a.exchange else (b, a)
                    long_book, short_book = (book_a, book_b) if long is a else (book_b, book_a)
                    exit = exit_quote(long_book, short_book, plan.qty_tokens)
                    quote.long, quote.short = long, short
                    quote.long_avg, quote.short_avg = entry.long_avg, entry.short_avg
                    quote.qty_tokens = plan.qty_tokens
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
                    if entry.roi_net_pct >= self._settings.min_roi_pct - self._settings.candidate_margin_pct and (
                        now - quote.capacity_ts_ms >= CAPACITY_EVERY_MS
                    ):
                        tokens = capacity_tokens(
                            long_book.asks,
                            short_book.bids,
                            self._fees(long, short),
                            self._settings.min_roi_pct,
                            record.assessment.common_step_tokens,
                        )
                        quote.capacity_usd = tokens * entry.long_avg
                        quote.capacity_ts_ms = int(now)
        self._quotes[record.key] = quote

        direction = quote.long.exchange if quote.long else None
        for episode_key in [k for k, pair in self._episode_pair.items() if pair == record.key]:
            if direction is None or not episode_key.endswith(f">{direction}"):
                # The other direction (or no measurement) sees no gap in this sample.
                self._advance(episode_key, record, Sample(int(now), None, None), None)
        if direction is not None:
            self._advance(f"{record.key}>{direction}", record, Sample(int(now), sample_roi, exit_spread), quote)

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
        block = None
        if record.blacklisted:
            block = "blacklisted"
        elif record.suspicious:
            block = "suspicious"
        elif quote is None:
            block = "no_book"
        elif quote.problem:
            block = quote.problem
        return {
            "key": record.key,
            "token": assessment.token,
            "long": None if long is None else {"exchange": long.exchange, "symbol": long.symbol_raw, "url": trade_url(long)},
            "short": None if short is None else {"exchange": short.exchange, "symbol": short.symbol_raw, "url": trade_url(short)},
            "long_avg": _text(quote.long_avg) if quote else None,
            "short_avg": _text(quote.short_avg) if quote else None,
            "qty_tokens": _text(quote.qty_tokens, 12) if quote else None,
            "roi_net_pct": _text(quote.roi_net_pct, 4) if quote else None,
            "roi_gross_pct": _text(quote.roi_gross_pct, 4) if quote else None,
            "roi_top_pct": None if top is None else round(top.roi_net_pct, 4),
            "exit_spread_pct": _text(quote.exit_spread_pct, 4) if quote else None,
            "capacity_usd": _text(quote.capacity_usd, 6) if quote else None,
            "age_long_ms": _ms(quote.age_long_ms) if quote else None,
            "age_short_ms": _ms(quote.age_short_ms) if quote else None,
            "phase": episode.phase.value if episode else None,
            "lifetime_ms": int(now - episode.first_entered_feed_ms) if episode and episode.first_entered_feed_ms else None,
            "roi_peak_pct": _text(episode.roi_peak, 4) if episode else None,
            "volume24h_weak_usd": _text(assessment.volume24h_weak_usd, 6),
            "suspicious": record.suspicious,
            "blacklisted": record.blacklisted,
            "block": block,
        }

    def _is_current_direction(self, episode_key: str) -> bool:
        quote = self._quotes.get(self._episode_pair[episode_key])
        return quote is not None and quote.long is not None and episode_key.endswith(f">{quote.long.exchange}")

    def _build_view(self, now: float) -> dict[str, Any]:
        tops_by_key = {top.key: top for top in self._tops}
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

        radar = []
        for top in self._tops:
            if len(radar) >= self._settings.radar_rows:
                break
            if top.key in in_feed_pairs:
                continue
            record = self._records[top.key]
            episode = next(
                (state for key, state in self._episodes.items() if self._episode_pair.get(key) == top.key), None
            )
            radar.append(self._row(record, self._quotes.get(top.key), top, now, episode))

        return {
            "rows": rows,
            "radar": radar,
            "settings": {
                "size_usd": _text(self._settings.size_usd),
                "min_roi_pct": _text(self._settings.min_roi_pct),
                "fresh_ms": self._settings.fresh_ms,
                "taker_fee_pct": {
                    "binance": _text(self._taker_fee_pct("binance")),
                    "mexc": _text(self._taker_fee_pct("mexc")),
                },
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
