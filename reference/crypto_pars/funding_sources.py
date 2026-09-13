"""Direct public-API funding scanner.

Every exchange rate is normalized to a percentage for an eight-hour period
before exchanges are compared.  The module uses public, read-only endpoints
and does not require exchange API keys.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

EXCHANGES = {
    "binance": "BINANCE",
    "bybit": "BYBIT",
    "okx": "OKX",
    "gate": "GATEIO",
    "mexc": "MEXC",
    "bitget": "BITGET",
    "kucoin": "KUCOIN",
    "bingx": "BINGX",
    "hyperliquid": "HYPERLIQUID",
    "phemex": "PHEMEX",
    "aster": "ASTER",
    "variational": "VARIATIONAL",
}

HOURS_PER_YEAR = 365 * 24
MAX_ABS_HOURLY_FUNDING_PCT = 4.0

SYMBOL_ALIASES = {
    "XBT": "BTC",
}


@dataclass(frozen=True, slots=True)
class FundingQuote:
    exchange: str
    symbol: str
    rate_8h_pct: float
    source_rate_pct: float
    interval_hours: float
    next_funding_time: int | None = None


FundingQuotes = dict[str, FundingQuote]
FundingFetcher = Callable[[httpx.AsyncClient], Awaitable[FundingQuotes]]


def exchange_url(exchange: str, symbol: str) -> str:
    s = symbol.upper()
    sl = symbol.lower()
    urls = {
        "binance": f"https://www.binance.com/en/futures/{s}USDT",
        "bybit": f"https://www.bybit.com/trade/usdt/{s}USDT",
        "okx": f"https://www.okx.com/trade-swap/{sl}-usdt-swap",
        "gate": f"https://www.gate.com/futures/USDT/{s}_USDT",
        "mexc": f"https://futures.mexc.com/exchange/{s}_USDT",
        "bitget": f"https://www.bitget.com/futures/usdt/{s}USDT",
        "kucoin": f"https://www.kucoin.com/futures/trade/{s}USDTM",
        "bingx": f"https://bingx.com/en/perpetual/{s}-USDT/",
        "hyperliquid": f"https://app.hyperliquid.xyz/trade/{s}",
        "phemex": f"https://phemex.com/futures/{s}-USDT",
        "aster": f"https://www.asterdex.com/en/trade/pro/futures/{s}USDT",
        "variational": f"https://omni.variational.io/perpetual/{s}",
    }
    return urls.get(exchange.lower(), "")


def normalize_symbol(raw: str) -> str | None:
    symbol = str(raw).upper().strip()
    for suffix in ("-USDT-SWAP", "_USDT", "-USDT", "USDTM", "USDT"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break

    symbol = SYMBOL_ALIASES.get(symbol, symbol)
    if not symbol or not symbol.replace("-", "").isalnum():
        return None
    if symbol.endswith("USD") or symbol[0].isdigit() or symbol.startswith((".", "@")):
        return None
    return symbol


def normalize_rate_8h(
    rate: float | str,
    interval_hours: float | str,
    *,
    rate_is_percent: bool = False,
) -> float:
    numeric_rate = float(rate)
    numeric_interval = float(interval_hours)
    if not math.isfinite(numeric_rate) or not math.isfinite(numeric_interval):
        raise ValueError("Funding rate and interval must be finite")
    if numeric_interval <= 0:
        raise ValueError("Funding interval must be positive")

    source_rate_pct = numeric_rate if rate_is_percent else numeric_rate * 100
    return source_rate_pct * (8 / numeric_interval)


def normalize_annualized_rate_8h(rate: float | str) -> float:
    """Convert an annualized decimal funding rate to an eight-hour percent."""
    numeric_rate = float(rate)
    if not math.isfinite(numeric_rate):
        raise ValueError("Annualized funding rate must be finite")
    return numeric_rate * 100 * (8 / HOURS_PER_YEAR)


def _quote(
    exchange: str,
    raw_symbol: str,
    rate: float | str,
    interval_hours: float | str,
    *,
    rate_is_percent: bool = False,
    next_funding_time: int | str | None = None,
) -> FundingQuote | None:
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        return None

    try:
        interval = float(interval_hours)
        source_rate_pct = float(rate) if rate_is_percent else float(rate) * 100
        normalized = normalize_rate_8h(
            rate,
            interval,
            rate_is_percent=rate_is_percent,
        )
        hourly_pct = source_rate_pct / interval
        if abs(hourly_pct) > MAX_ABS_HOURLY_FUNDING_PCT:
            log.warning(
                "[funding] %s/%s: rejected %.6f%% hourly rate above %.6f%% cap",
                exchange,
                symbol,
                hourly_pct,
                MAX_ABS_HOURLY_FUNDING_PCT,
            )
            return None
        next_time = int(next_funding_time) if next_funding_time not in (None, "") else None
    except (TypeError, ValueError):
        return None

    return FundingQuote(
        exchange=exchange,
        symbol=symbol,
        rate_8h_pct=normalized,
        source_rate_pct=source_rate_pct,
        interval_hours=interval,
        next_funding_time=next_time,
    )


def _annualized_quote(
    exchange: str,
    raw_symbol: str,
    annualized_rate: float | str,
    interval_hours: float | str,
) -> FundingQuote | None:
    """Build a quote from a venue rate expressed as an annualized decimal."""
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        return None

    try:
        numeric_rate = float(annualized_rate)
        interval = float(interval_hours)
        if not math.isfinite(numeric_rate) or not math.isfinite(interval):
            raise ValueError
        if interval <= 0:
            raise ValueError

        hourly_pct = numeric_rate * 100 / HOURS_PER_YEAR
        if abs(hourly_pct) > MAX_ABS_HOURLY_FUNDING_PCT:
            log.warning(
                "[funding] %s/%s: rejected %.6f%% hourly rate above %.6f%% cap",
                exchange,
                symbol,
                hourly_pct,
                MAX_ABS_HOURLY_FUNDING_PCT,
            )
            return None
    except (TypeError, ValueError):
        return None

    return FundingQuote(
        exchange=exchange,
        symbol=symbol,
        rate_8h_pct=normalize_annualized_rate_8h(numeric_rate),
        source_rate_pct=hourly_pct * interval,
        interval_hours=interval,
    )


def _response_json(response: httpx.Response):
    response.raise_for_status()
    return response.json()


async def fetch_binance(client: httpx.AsyncClient) -> FundingQuotes:
    rates_response, info_response = await asyncio.gather(
        client.get("https://fapi.binance.com/fapi/v1/premiumIndex"),
        client.get("https://fapi.binance.com/fapi/v1/fundingInfo"),
    )
    rates = _response_json(rates_response)
    interval_by_symbol = {
        item["symbol"]: float(item.get("fundingIntervalHours", 8))
        for item in _response_json(info_response)
        if item.get("symbol")
    }

    quotes = {}
    for item in rates:
        raw_symbol = item.get("symbol", "")
        if not raw_symbol.endswith("USDT") or item.get("lastFundingRate") in (None, ""):
            continue
        quote = _quote(
            "binance",
            raw_symbol,
            item["lastFundingRate"],
            interval_by_symbol.get(raw_symbol, 8),
            next_funding_time=item.get("nextFundingTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_bybit(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear"},
    )
    items = _response_json(response).get("result", {}).get("list", [])

    quotes = {}
    for item in items:
        raw_symbol = item.get("symbol", "")
        if not raw_symbol.endswith("USDT") or item.get("fundingRate") in (None, ""):
            continue
        quote = _quote(
            "bybit",
            raw_symbol,
            item["fundingRate"],
            item.get("fundingIntervalHour", 8),
            next_funding_time=item.get("nextFundingTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def _fetch_okx_quote(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    instrument_id: str,
) -> FundingQuote | None:
    async with semaphore:
        try:
            response = await client.get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": instrument_id},
            )
            payload = _response_json(response)
            if payload.get("code") != "0" or not payload.get("data"):
                return None
            item = payload["data"][0]
            current_time = int(item.get("fundingTime", 0))
            next_time = int(item.get("nextFundingTime", 0))
            interval_hours = (next_time - current_time) / 3_600_000
            if interval_hours <= 0:
                interval_hours = 8
            return _quote(
                "okx",
                instrument_id,
                item["fundingRate"],
                interval_hours,
                next_funding_time=next_time,
            )
        except Exception as exc:
            log.debug("[funding] okx/%s: %s", instrument_id, exc)
            return None


async def fetch_okx(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get(
        "https://www.okx.com/api/v5/public/instruments",
        params={"instType": "SWAP"},
    )
    instruments = [
        item["instId"]
        for item in _response_json(response).get("data", [])
        if item.get("state") == "live" and item.get("instId", "").endswith("-USDT-SWAP")
    ]

    semaphore = asyncio.Semaphore(30)
    results = await asyncio.gather(
        *[_fetch_okx_quote(client, semaphore, instrument_id) for instrument_id in instruments]
    )
    return {quote.symbol: quote for quote in results if quote}


async def fetch_gate(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    items = _response_json(response)

    quotes = {}
    for item in items:
        if item.get("in_delisting") or item.get("funding_rate") in (None, ""):
            continue
        quote = _quote(
            "gate",
            item.get("name", ""),
            item["funding_rate"],
            float(item.get("funding_interval", 28_800)) / 3_600,
            next_funding_time=(
                int(item["funding_next_apply"]) * 1_000
                if item.get("funding_next_apply")
                else None
            ),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_mexc(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get("https://contract.mexc.com/api/v1/contract/funding_rate")
    items = _response_json(response).get("data", [])

    quotes = {}
    for item in items:
        if item.get("fundingRate") in (None, ""):
            continue
        quote = _quote(
            "mexc",
            item.get("symbol", ""),
            item["fundingRate"],
            item.get("collectCycle", 8),
            next_funding_time=item.get("nextSettleTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_bitget(client: httpx.AsyncClient) -> FundingQuotes:
    tickers_response, contracts_response = await asyncio.gather(
        client.get(
            "https://api.bitget.com/api/v2/mix/market/tickers",
            params={"productType": "USDT-FUTURES"},
        ),
        client.get(
            "https://api.bitget.com/api/v2/mix/market/contracts",
            params={"productType": "USDT-FUTURES"},
        ),
    )
    tickers = _response_json(tickers_response).get("data", [])
    interval_by_symbol = {
        item["symbol"]: float(item.get("fundInterval", 8))
        for item in _response_json(contracts_response).get("data", [])
        if item.get("symbol")
    }

    quotes = {}
    for item in tickers:
        raw_symbol = item.get("symbol", "")
        if not raw_symbol.endswith("USDT") or item.get("fundingRate") in (None, ""):
            continue
        quote = _quote(
            "bitget",
            raw_symbol,
            item["fundingRate"],
            interval_by_symbol.get(raw_symbol, 8),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_kucoin(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get("https://api-futures.kucoin.com/api/v1/contracts/active")
    items = _response_json(response).get("data", [])

    quotes = {}
    for item in items:
        if item.get("settleCurrency") != "USDT" or item.get("fundingFeeRate") in (None, ""):
            continue
        interval_ms = item.get("currentFundingRateGranularity") or 28_800_000
        interval_hours = float(interval_ms) / 3_600_000
        quote = _quote(
            "kucoin",
            item.get("symbol", ""),
            item["fundingFeeRate"],
            interval_hours,
            next_funding_time=item.get("nextFundingRateDateTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_bingx(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get(
        "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
    )
    items = _response_json(response).get("data", [])

    quotes = {}
    for item in items:
        if item.get("lastFundingRate") in (None, ""):
            continue
        quote = _quote(
            "bingx",
            item.get("symbol", ""),
            item["lastFundingRate"],
            item.get("fundingIntervalHours", 8),
            next_funding_time=item.get("nextFundingTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_hyperliquid(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
    )
    meta, contexts = _response_json(response)

    quotes = {}
    for asset, context in zip(meta.get("universe", []), contexts):
        if context.get("funding") in (None, ""):
            continue
        quote = _quote(
            "hyperliquid",
            asset.get("name", ""),
            context["funding"],
            1,
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_phemex(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get(
        "https://api.phemex.com/contract-biz/public/real-funding-rates",
        params={"pageNum": 1, "pageSize": 1_000},
    )
    items = _response_json(response).get("data", {}).get("rows", [])

    quotes = {}
    for item in items:
        if item.get("fundingRate") in (None, ""):
            continue
        quote = _quote(
            "phemex",
            item.get("symbol", ""),
            item["fundingRate"],
            float(item.get("fundingInterval", 28_800)) / 3_600,
            next_funding_time=item.get("nextfundingTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_aster(client: httpx.AsyncClient) -> FundingQuotes:
    rates_response, info_response = await asyncio.gather(
        client.get("https://fapi.asterdex.com/fapi/v1/premiumIndex"),
        client.get("https://fapi.asterdex.com/fapi/v1/fundingInfo"),
    )
    rates = _response_json(rates_response)
    interval_by_symbol = {
        item["symbol"]: float(item.get("fundingIntervalHours", 8))
        for item in _response_json(info_response)
        if item.get("symbol")
    }

    quotes = {}
    for item in rates:
        raw_symbol = item.get("symbol", "")
        if not raw_symbol.endswith("USDT") or item.get("lastFundingRate") in (None, ""):
            continue
        quote = _quote(
            "aster",
            raw_symbol,
            item["lastFundingRate"],
            interval_by_symbol.get(raw_symbol, 8),
            next_funding_time=item.get("nextFundingTime"),
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


async def fetch_variational(client: httpx.AsyncClient) -> FundingQuotes:
    response = await client.get(
        "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
    )
    items = _response_json(response).get("listings", [])

    quotes = {}
    for item in items:
        if item.get("funding_rate") in (None, ""):
            continue
        quote = _annualized_quote(
            "variational",
            item.get("ticker", ""),
            item["funding_rate"],
            float(item.get("funding_interval_s", 28_800)) / 3_600,
        )
        if quote:
            quotes[quote.symbol] = quote
    return quotes


FETCHERS: dict[str, FundingFetcher] = {
    "binance": fetch_binance,
    "bybit": fetch_bybit,
    "okx": fetch_okx,
    "gate": fetch_gate,
    "mexc": fetch_mexc,
    "bitget": fetch_bitget,
    "kucoin": fetch_kucoin,
    "bingx": fetch_bingx,
    "hyperliquid": fetch_hyperliquid,
    "phemex": fetch_phemex,
    "aster": fetch_aster,
    "variational": fetch_variational,
}
