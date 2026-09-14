"""
VFP: Variational Omni behind the adapter contract, read-only — its listings as instruments, its size-tiered quotes as prices and a two-level book, and a link probe.
Changes when: Variational publishes a trading API or changes the statistics endpoint.
Anti-goal:
1. Pretending to trade — every account and order method refuses; the pre-trade check blocks Variational pairs.
2. Breaking the public rate limit (10 requests per 10 seconds per IP) — one cached, single-flight request serves the
   market feed, the catalog and the link probe alike.
3. Inventing depth — the book has exactly the two points Variational quotes ($1k and $100k); bigger sizes are too thin.

Endpoint: GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats (docs/EXCHANGES.md, section Variational).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

import aiohttp
import orjson

from app.core.account import AccountFacts
from app.core.pairs import Quote
from app.core.schemas import Instrument
from app.core.symbols import parse_symbol
from app.exchanges.base import Balance, ClockProbe, FillReport, OrderReport, Position

STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
HEADERS = {"User-Agent": "terminal-ludik/0.1", "Accept": "application/json"}
QTY_STEP_TOKENS = Decimal("0.00000001")
TIER_SMALL_USD = Decimal(1_000)
TIER_LARGE_USD = Decimal(100_000)
NOT_TRADABLE = "Variational Omni has no trading API yet: pairs with Variational are shown for information only"


class ReadOnlyExchange(RuntimeError):
    pass


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def parse_instruments(stats: dict[str, Any]) -> list[Instrument]:
    """Listings are bare tickers; a numeric prefix is a multiplier (1000PEPE: price per 1000 PEPE)."""
    instruments = []
    for item in stats.get("listings") or []:
        ticker = str(item.get("ticker") or "")
        parsed = parse_symbol(ticker, "variational")
        if parsed is None or _positive(item.get("mark_price")) is None:
            continue
        instruments.append(
            Instrument(
                exchange="variational",
                symbol_raw=ticker,
                token=parsed.token,
                qty_unit_tokens=parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=QTY_STEP_TOKENS,
                min_qty_units=Decimal(0),
                max_market_qty_units=None,
                min_notional_usd=Decimal(0),
                price_tick=None,
            )
        )
    return instruments


def parse_quotes(stats: dict[str, Any]) -> dict[str, Quote]:
    """Best prices are the base quotes; mark_price has no separate index, volume_24h is in USD."""
    quotes = {}
    for item in stats.get("listings") or []:
        base = (item.get("quotes") or {}).get("base") or {}
        quotes[str(item.get("ticker"))] = Quote(
            bid=_positive(base.get("bid")),
            ask=_positive(base.get("ask")),
            mark=_positive(item.get("mark_price")),
            index=None,
            volume24h_usd=_positive(item.get("volume_24h")),
        )
    return quotes


@dataclass(frozen=True, slots=True)
class TieredQuote:
    ticker: str
    best_bid: float | None
    best_ask: float | None
    bids: list[list[str]]
    asks: list[list[str]]
    mark: float | None
    volume24h_usd: float | None
    updated_ms: int


def tier_levels(small: Decimal | None, large: Decimal | None) -> list[list[str]]:
    """
    Two price levels in quote units whose walked average equals the $1k quote at $1k and the $100k quote at $100k:
    level 1 holds $1k at the $1k price; level 2 holds the remaining $99k at the price that makes the $100k average right.
    """
    if small is None:
        return []
    first_units = TIER_SMALL_USD / small
    levels = [[str(small), str(first_units)]]
    if large is None:
        return levels
    total_units = TIER_LARGE_USD / large
    second_units = total_units - first_units
    if second_units <= 0:
        return levels
    second_price = (TIER_LARGE_USD - TIER_SMALL_USD) / second_units
    levels.append([str(second_price), str(second_units)])
    return levels


def parse_tiered(stats: dict[str, Any]) -> list[TieredQuote]:
    quotes = []
    for item in stats.get("listings") or []:
        tiers = item.get("quotes") or {}
        base, small, large = tiers.get("base") or {}, tiers.get("size_1k") or {}, tiers.get("size_100k") or {}
        updated = _updated_ms(tiers.get("updated_at"))
        quotes.append(
            TieredQuote(
                ticker=str(item.get("ticker")),
                best_bid=float(base["bid"]) if _positive(base.get("bid")) else None,
                best_ask=float(base["ask"]) if _positive(base.get("ask")) else None,
                bids=tier_levels(_positive(small.get("bid")), _positive(large.get("bid"))),
                asks=tier_levels(_positive(small.get("ask")), _positive(large.get("ask"))),
                mark=float(item["mark_price"]) if _positive(item.get("mark_price")) else None,
                volume24h_usd=float(item["volume_24h"]) if _positive(item.get("volume_24h")) else None,
                updated_ms=updated,
            )
        )
    return quotes


def _updated_ms(value: Any) -> int:
    """RFC 3339 with nanoseconds ("2026-09-14T08:01:11.466733379Z") to epoch milliseconds; 0 when unparseable."""
    if not value:
        return 0
    text = str(value).replace("Z", "+00:00")
    # Nanosecond fractions do not fit datetime; keep microseconds and the zone that follows the fraction.
    if "." in text:
        head, rest = text.split(".", 1)
        count = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:count][:6]}{rest[count:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        return 0  # a timestamp without a zone cannot be trusted to be UTC
    return int(parsed.timestamp() * 1000)


class VariationalAdapter:
    name = "variational"
    read_only = True

    def __init__(self, timeout_s: float = 10, min_interval_ms: int = 2000) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._min_interval_s = min_interval_ms / 1000
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._stats: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self.last_latency_ms: int | None = None

    async def stats(self, max_age_s: float | None = None) -> dict[str, Any]:
        """The latest statistics, fetched at most once per min_interval no matter how many callers ask."""
        max_age = self._min_interval_s if max_age_s is None else max(max_age_s, self._min_interval_s)
        async with self._lock:
            if self._stats is not None and time.monotonic() - self._fetched_at < max_age:
                return self._stats
            if self._session is None or self._session.closed:
                # Cloudflare in front of the API answers 403 to some default client names (Python-urllib); name ours.
                self._session = aiohttp.ClientSession(timeout=self._timeout, headers=HEADERS)
            started = time.monotonic()
            async with self._session.get(STATS_URL) as response:
                response.raise_for_status()
                self._stats = orjson.loads(await response.read())
            self._fetched_at = time.monotonic()
            self.last_latency_ms = round((self._fetched_at - started) * 1000)
            return self._stats

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self.stats(max_age_s=60))

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(await self.stats(max_age_s=60))

    async def probe_clock(self) -> ClockProbe:
        # The market feed keeps the statistics fresh; the probe reads that and adds no request of its own.
        await self.stats(max_age_s=30)
        return ClockProbe(ping_ms=self.last_latency_ms or 0, clock_offset_ms=None, server_ts_ms=None)

    async def check_account(self) -> AccountFacts:
        return AccountFacts(errors=(NOT_TRADABLE,))

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        return []

    async def fetch_balance(self) -> Balance:
        raise ReadOnlyExchange(NOT_TRADABLE)

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raise ReadOnlyExchange(NOT_TRADABLE)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        raise ReadOnlyExchange(NOT_TRADABLE)

    async def place_market_order(self, *args: Any, **kwargs: Any) -> OrderReport:
        raise ReadOnlyExchange(NOT_TRADABLE)

    async def fetch_order(self, *args: Any, **kwargs: Any) -> OrderReport:
        raise ReadOnlyExchange(NOT_TRADABLE)

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        raise ReadOnlyExchange(NOT_TRADABLE)
