"""Funding opportunity orchestration and comparison logic."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

import httpx

import rankings
from funding_sources import (
    EXCHANGES,
    FETCHERS,
    FundingFetcher,
    FundingQuote,
    FundingQuotes,
    exchange_url,
    normalize_annualized_rate_8h,
    normalize_rate_8h,
    normalize_symbol,
)

log = logging.getLogger(__name__)

FUNDING_PROFIT_OPTIONS = (2.0, 3.5, 5.0, 7.0)

_futures_universe_cache: dict[str, set[str]] = {}
_futures_universe_cached_at: datetime | None = None
_futures_universe_selection: frozenset[str] | None = None


def normalize_profit_threshold(value: float) -> float:
    """Map legacy values onto the new scale, rounding upward when possible."""
    for option in FUNDING_PROFIT_OPTIONS:
        if value <= option:
            return option
    return FUNDING_PROFIT_OPTIONS[-1]


async def _fetch_with_retry(
    name: str,
    fetcher: FundingFetcher,
    client: httpx.AsyncClient,
    retries: int = 1,
) -> tuple[str, FundingQuotes | Exception]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return name, await fetcher(client)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(2**attempt)
    return name, last_error or RuntimeError(f"{name} failed without an exception")


async def fetch_all_funding(
    allowed_exchanges: set[str] | None = None,
) -> dict[str, FundingQuotes]:
    global _futures_universe_cache
    global _futures_universe_cached_at
    global _futures_universe_selection

    selected = {
        name: fetcher
        for name, fetcher in FETCHERS.items()
        if not allowed_exchanges or name in allowed_exchanges
    }
    limits = httpx.Limits(max_connections=60, max_keepalive_connections=20)
    headers = {"User-Agent": "crypto-pars/2.0"}
    async with httpx.AsyncClient(timeout=25, limits=limits, headers=headers) as client:
        results = await asyncio.gather(
            *[
                _fetch_with_retry(name, fetcher, client)
                for name, fetcher in selected.items()
            ]
        )

    quotes_by_exchange = {}
    for exchange, result in results:
        if isinstance(result, Exception):
            log.warning("[funding] %s: %s", exchange, result)
            continue
        quotes_by_exchange[exchange] = result
        log.info("[funding] %s: %s symbols", exchange, len(result))

    _futures_universe_cache = {
        exchange: set(quotes)
        for exchange, quotes in quotes_by_exchange.items()
    }
    _futures_universe_cached_at = datetime.now()
    _futures_universe_selection = frozenset(selected)
    return quotes_by_exchange


async def get_futures_universe(
    allowed_exchanges: set[str] | None = None,
    *,
    max_age_seconds: int = 3600,
) -> dict[str, set[str]]:
    """Return futures symbols by exchange, reusing a recent funding scan."""
    selected = frozenset(
        name
        for name in FETCHERS
        if not allowed_exchanges or name in allowed_exchanges
    )
    cache_is_fresh = (
        _futures_universe_cached_at is not None
        and _futures_universe_selection == selected
        and (datetime.now() - _futures_universe_cached_at).total_seconds()
        <= max_age_seconds
    )
    if cache_is_fresh:
        return {
            exchange: set(symbols)
            for exchange, symbols in _futures_universe_cache.items()
        }

    quotes = await fetch_all_funding(allowed_exchanges)
    return {
        exchange: set(exchange_quotes)
        for exchange, exchange_quotes in quotes.items()
    }


def find_opportunities(
    quotes_by_exchange: dict[str, FundingQuotes],
    min_spread_pct: float = 1.0,
    min_rate_abs: float = 0.0,
) -> list[dict]:
    symbols = {
        symbol
        for exchange_quotes in quotes_by_exchange.values()
        for symbol in exchange_quotes
    }

    opportunities = []
    for symbol in symbols:
        quotes = {
            exchange: exchange_quotes[symbol]
            for exchange, exchange_quotes in quotes_by_exchange.items()
            if symbol in exchange_quotes
        }
        if len(quotes) < 2:
            continue

        long_exchange = min(quotes, key=lambda exchange: quotes[exchange].rate_8h_pct)
        short_exchange = max(quotes, key=lambda exchange: quotes[exchange].rate_8h_pct)
        long_rate = quotes[long_exchange].rate_8h_pct
        short_rate = quotes[short_exchange].rate_8h_pct
        spread = short_rate - long_rate

        if spread < min_spread_pct:
            continue
        if min_rate_abs > 0 and abs(long_rate) < min_rate_abs and abs(short_rate) < min_rate_abs:
            continue

        opportunities.append(
            {
                "symbol": symbol,
                "profit_8h": round(spread, 6),
                "long_exchange": long_exchange,
                "long_rate": round(long_rate, 6),
                "short_exchange": short_exchange,
                "short_rate": round(short_rate, 6),
                "all_rates": {
                    exchange: round(quote.rate_8h_pct, 6)
                    for exchange, quote in sorted(
                        quotes.items(),
                        key=lambda item: item[1].rate_8h_pct,
                    )
                },
            }
        )

    opportunities.sort(key=lambda item: item["profit_8h"], reverse=True)
    return opportunities


def filter_opportunities(
    opportunities: list[dict],
    min_spread_pct: float,
    allowed_tiers: set[str],
) -> list[dict]:
    return [
        opportunity
        for opportunity in opportunities
        if opportunity["profit_8h"] >= min_spread_pct
        and rankings.get_grade(opportunity["symbol"]) in allowed_tiers
    ]


def _exchange_link(exchange: str, symbol: str) -> str:
    display = html.escape(EXCHANGES.get(exchange, exchange.upper()))
    url = html.escape(exchange_url(exchange, symbol), quote=True)
    return f'<a href="{url}">{display}</a>' if url else display


def format_opportunity(opportunity: dict, index: int) -> str:
    raw_symbol = opportunity["symbol"]
    symbol = html.escape(raw_symbol)
    rank = rankings.get_rank(raw_symbol)
    grade = rankings.get_grade(raw_symbol)
    rank_label = f"#{rank}" if rank is not None else "без ранга"
    grade_label = f"{rankings.grade_emoji(grade)} Тир {grade} ({rank_label})"
    long_link = _exchange_link(opportunity["long_exchange"], raw_symbol)
    short_link = _exchange_link(opportunity["short_exchange"], raw_symbol)

    lines = [
        (
            f"<b>#{index} {symbol}</b>  | Фандинг "
            f"<b>{opportunity['profit_8h']:.3f}%</b> | {grade_label}"
        ),
        "",
        (
            f"🟢 <b>LONG</b>  → {long_link}  "
            f"({opportunity['long_rate']:+.3f}%)"
        ),
        (
            f"🔴 <b>SHORT</b> → {short_link}  "
            f"({opportunity['short_rate']:+.3f}%)"
        ),
        "",
        "📊 <b>Другие биржи:</b>",
    ]
    for exchange, rate in opportunity["all_rates"].items():
        lines.append(f"  • {_exchange_link(exchange, raw_symbol)}: {rate:+.3f}%")
    lines.append("\n<i>by @crypto_ludiki</i>")
    return "\n".join(lines)


async def scan_funding_opportunities(
    min_spread_pct: float = 1.0,
    min_rate_abs: float = 0.0,
    allowed_exchanges: set[str] | None = None,
) -> list[dict] | None:
    await rankings.ensure_fresh()
    quotes = await fetch_all_funding(allowed_exchanges)
    if len(quotes) < 2:
        log.error("[funding] Not enough exchanges responded")
        return None

    opportunities = find_opportunities(quotes, min_spread_pct, min_rate_abs)
    log.info(
        "[funding] Found %s opportunities with spread >= %s%%/8h",
        len(opportunities),
        min_spread_pct,
    )
    return opportunities


__all__ = [
    "EXCHANGES",
    "FUNDING_PROFIT_OPTIONS",
    "FundingQuote",
    "exchange_url",
    "fetch_all_funding",
    "filter_opportunities",
    "find_opportunities",
    "format_opportunity",
    "get_futures_universe",
    "normalize_annualized_rate_8h",
    "normalize_profit_threshold",
    "normalize_rate_8h",
    "normalize_symbol",
    "scan_funding_opportunities",
]
