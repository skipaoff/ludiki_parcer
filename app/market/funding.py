"""
VFP: Current funding rates of every contract on every connected exchange — rate per settlement, interval and next settlement time — kept fresh in memory.
Changes when: an exchange changes where or how it publishes funding, or a new exchange is connected.
Anti-goal:
1. Serving an old rate as current — an exchange whose rates have not refreshed for STALE_AFTER_S reports no rates at all.
2. Rejecting whole answers for one odd contract — absurd rates (over 4% per hour) are dropped one by one.
3. Hammering heavy endpoints — rates are polled once a minute; Gate's 1.2 MB contract list (schedules only) twice an hour.

Sources checked live on 15.09.2026 (docs/EXCHANGES.md, section «Фандинг»).
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
import orjson

from app.core.funding import FundingRate

log = logging.getLogger(__name__)

POLL_S = 60
SCHEDULE_POLL_S = 1800
STALE_AFTER_S = 900
MAX_ABS_HOURLY_PCT = Decimal("4")
HOURS_PER_YEAR = Decimal(365 * 24)
HEADERS = {"User-Agent": "terminal-ludik/0.1", "Accept": "application/json"}

URLS = {
    "binance_premium": "https://fapi.binance.com/fapi/v1/premiumIndex",
    "binance_info": "https://fapi.binance.com/fapi/v1/fundingInfo",
    "aster_premium": "https://fapi.asterdex.com/fapi/v1/premiumIndex",
    "aster_info": "https://fapi.asterdex.com/fapi/v1/fundingInfo",
    "mexc": "https://api.mexc.com/api/v1/contract/funding_rate",
    "gate_tickers": "https://api.gateio.ws/api/v4/futures/usdt/tickers",
    "gate_contracts": "https://api.gateio.ws/api/v4/futures/usdt/contracts",
    "bingx": "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex",
    "bybit": "https://api.bybit.com/v5/market/tickers?category=linear",
    "bitget": "https://api.bitget.com/api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES",
    "kucoin": "https://api-futures.kucoin.com/api/v1/contracts/active",
    "hyperliquid": "https://api.hyperliquid.xyz/info",
}

Rates = dict[str, FundingRate]


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _rate(fraction: Any, interval_hours: Any, next_ms: Any) -> FundingRate | None:
    """A rate given as a fraction per settlement; None for missing or absurd values."""
    rate, hours = _decimal(fraction), _decimal(interval_hours)
    if rate is None or hours is None or hours <= 0:
        return None
    result = FundingRate(rate_pct=rate * 100, interval_hours=hours, next_ms=int(next_ms) if next_ms not in (None, "", 0, "0") else None)
    return result if abs(result.hourly_pct) <= MAX_ABS_HOURLY_PCT else None


def _collect(items: Iterable[tuple[str, FundingRate | None]]) -> Rates:
    return {symbol: rate for symbol, rate in items if symbol and rate is not None}


def parse_binance_like(premium: list[dict[str, Any]], info: list[dict[str, Any]]) -> Rates:
    """premiumIndex: lastFundingRate, nextFundingTime; fundingInfo lists only contracts whose interval is not the default 8 h."""
    intervals = {item["symbol"]: item.get("fundingIntervalHours", 8) for item in info if item.get("symbol")}
    return _collect(
        (item.get("symbol", ""), _rate(item.get("lastFundingRate"), intervals.get(item.get("symbol"), 8), item.get("nextFundingTime")))
        for item in premium
    )


def parse_mexc(payload: dict[str, Any]) -> Rates:
    """GET /api/v1/contract/funding_rate: fundingRate, collectCycle (hours), nextSettleTime."""
    if not payload.get("success", False):
        raise RuntimeError(f"mexc funding answer code {payload.get('code')}")
    return _collect(
        (item.get("symbol", ""), _rate(item.get("fundingRate"), item.get("collectCycle", 8), item.get("nextSettleTime")))
        for item in payload.get("data") or []
    )


def parse_gate_schedule(contracts: list[dict[str, Any]]) -> dict[str, tuple[int, int | None]]:
    """GET /futures/usdt/contracts: funding_interval (s) and funding_next_apply (s) by contract."""
    schedule = {}
    for item in contracts:
        name = item.get("name")
        interval = item.get("funding_interval")
        if not name or not interval:
            continue
        next_apply = item.get("funding_next_apply")
        schedule[name] = (int(interval), int(float(next_apply) * 1000) if next_apply else None)
    return schedule


def parse_gate(tickers: list[dict[str, Any]], schedule: dict[str, tuple[int, int | None]]) -> Rates:
    """GET /futures/usdt/tickers: funding_rate; interval and next settlement come from the contract list."""
    rates = []
    for item in tickers:
        contract = item.get("contract", "")
        interval_s, next_ms = schedule.get(contract, (28_800, None))
        rates.append((contract, _rate(item.get("funding_rate"), Decimal(interval_s) / 3600, next_ms)))
    return _collect(rates)


def parse_bingx(payload: dict[str, Any]) -> Rates:
    """GET /openApi/swap/v2/quote/premiumIndex: lastFundingRate, fundingIntervalHours, nextFundingTime."""
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(f"bingx premium index answer code {payload.get('code')}")
    return _collect(
        (item.get("symbol", ""), _rate(item.get("lastFundingRate"), item.get("fundingIntervalHours", 8), item.get("nextFundingTime")))
        for item in payload.get("data") or []
    )


def parse_bybit(payload: dict[str, Any]) -> Rates:
    """GET /v5/market/tickers?category=linear: fundingRate, fundingIntervalHour, nextFundingTime."""
    if payload.get("retCode") not in (0, "0"):
        raise RuntimeError(f"bybit tickers answer {payload.get('retCode')}")
    return _collect(
        (item.get("symbol", ""), _rate(item.get("fundingRate"), item.get("fundingIntervalHour") or 8, item.get("nextFundingTime")))
        for item in (payload.get("result") or {}).get("list") or []
    )


def parse_bitget(payload: dict[str, Any]) -> Rates:
    """GET /api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES: fundingRate, fundingRateInterval (hours), nextUpdate."""
    if str(payload.get("code")) != "00000":
        raise RuntimeError(f"bitget funding answer {payload.get('code')}")
    return _collect(
        (item.get("symbol", ""), _rate(item.get("fundingRate"), item.get("fundingRateInterval") or 8, item.get("nextUpdate")))
        for item in payload.get("data") or []
    )


def parse_kucoin(payload: dict[str, Any]) -> Rates:
    """GET /api/v1/contracts/active: fundingFeeRate, currentFundingRateGranularity (ms), nextFundingRateDateTime."""
    if str(payload.get("code")) != "200000":
        raise RuntimeError(f"kucoin contracts answer {payload.get('code')}")
    rates = []
    for item in payload.get("data") or []:
        granularity = _decimal(item.get("currentFundingRateGranularity") or item.get("fundingRateGranularity"))
        hours = granularity / 3_600_000 if granularity else None
        rates.append((item.get("symbol", ""), _rate(item.get("fundingFeeRate"), hours, item.get("nextFundingRateDateTime"))))
    return _collect(rates)


def parse_hyperliquid(answer: list[Any], now_ms: int) -> Rates:
    """
    POST /info {"type": "metaAndAssetCtxs"}: funding is the hourly rate of each coin; settlements fall on every whole hour
    (UTC), so the next one is the coming hour.
    """
    meta, contexts = answer[0], answer[1]
    next_hour = (now_ms // 3_600_000 + 1) * 3_600_000
    return _collect(
        (coin.get("name", ""), _rate(context.get("funding"), 1, next_hour))
        for coin, context in zip(meta.get("universe") or [], contexts)
        if not coin.get("isDelisted")
    )


def parse_variational(stats: dict[str, Any]) -> Rates:
    """
    GET /metadata/stats: funding_rate is an annualised fraction (0.1095 = 10.95% a year), funding_interval_s the interval.
    No settlement times are published, so funding accrues in proportion to time.
    """
    rates = []
    for item in stats.get("listings") or []:
        annual = _decimal(item.get("funding_rate"))
        interval_s = _decimal(item.get("funding_interval_s")) or Decimal(3600)  # 0 means no fixed interval
        if annual is None:
            continue
        hours = interval_s / 3600
        rates.append((item.get("ticker", ""), _rate(annual * hours / HOURS_PER_YEAR, hours, None)))
    return _collect(rates)


class FundingService:
    def __init__(
        self,
        exchanges: Iterable[str],
        variational_stats: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._exchanges = [name for name in exchanges]
        self._variational_stats = variational_stats
        self._clock = clock
        self._rates: dict[str, Rates] = {}
        self._updated_s: dict[str, float] = {}
        self._errors: dict[str, int] = {}
        self._schedules: dict[str, tuple[float, Any]] = {}
        self._last_error_log: dict[str, float] = {}

    def rate(self, exchange: str, symbol: str) -> FundingRate | None:
        updated = self._updated_s.get(exchange)
        if updated is None or self._clock() - updated > STALE_AFTER_S:
            return None
        return self._rates.get(exchange, {}).get(symbol)

    def store(self, exchange: str, rates: Rates) -> None:
        self._rates[exchange] = rates
        self._updated_s[exchange] = self._clock()

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        return {
            name: {
                "contracts": len(self._rates.get(name, {})),
                "age_s": None if name not in self._updated_s else round(now - self._updated_s[name]),
                "errors": self._errors.get(name, 0),
            }
            for name in self._exchanges
        }

    async def run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=HEADERS) as session:
            loops = [self._loop(name, session) for name in self._exchanges if name in FETCHERS or name == "variational"]
            await asyncio.gather(*loops)

    async def _loop(self, exchange: str, session: aiohttp.ClientSession) -> None:
        while True:
            try:
                if exchange == "variational":
                    if self._variational_stats is None:
                        return
                    rates = parse_variational(await self._variational_stats())
                else:
                    rates = await FETCHERS[exchange](self, session)
                self.store(exchange, rates)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors[exchange] = self._errors.get(exchange, 0) + 1
                if time.monotonic() - self._last_error_log.get(exchange, 0) > 300:
                    self._last_error_log[exchange] = time.monotonic()
                    log.warning("%s funding poll failed: %s: %s", exchange, type(exc).__name__, exc)
            await asyncio.sleep(POLL_S)

    async def _schedule(self, name: str, session: aiohttp.ClientSession) -> Any:
        """Intervals and settlement schedules change rarely and some are heavy: fetched at most every SCHEDULE_POLL_S."""
        cached = self._schedules.get(name)
        if cached is None or time.monotonic() - cached[0] > SCHEDULE_POLL_S:
            cached = (time.monotonic(), await _get(session, URLS[name]))
            self._schedules[name] = cached
        return cached[1]

    async def _binance_like(self, prefix: str, session: aiohttp.ClientSession) -> Rates:
        info = await self._schedule(f"{prefix}_info", session)
        return parse_binance_like(await _get(session, URLS[f"{prefix}_premium"]), info)

    async def _gate(self, session: aiohttp.ClientSession) -> Rates:
        schedule = parse_gate_schedule(await self._schedule("gate_contracts", session))
        return parse_gate(await _get(session, URLS["gate_tickers"]), schedule)


async def _get(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as response:
        response.raise_for_status()
        return orjson.loads(await response.read())


async def _mexc(service: FundingService, session: aiohttp.ClientSession) -> Rates:
    return parse_mexc(await _get(session, URLS["mexc"]))


async def _bingx(service: FundingService, session: aiohttp.ClientSession) -> Rates:
    return parse_bingx(await _get(session, URLS["bingx"]))


async def _bybit(service: FundingService, session: aiohttp.ClientSession) -> Rates:
    return parse_bybit(await _get(session, URLS["bybit"]))


async def _bitget(service: FundingService, session: aiohttp.ClientSession) -> Rates:
    return parse_bitget(await _get(session, URLS["bitget"]))


async def _kucoin(service: FundingService, session: aiohttp.ClientSession) -> Rates:
    return parse_kucoin(await _get(session, URLS["kucoin"]))


async def _hyperliquid(service: FundingService, session: aiohttp.ClientSession) -> Rates:
    async with session.post(URLS["hyperliquid"], json={"type": "metaAndAssetCtxs"}) as response:
        response.raise_for_status()
        answer = orjson.loads(await response.read())
    return parse_hyperliquid(answer, int(service._clock() * 1000))


FETCHERS: dict[str, Callable[[FundingService, aiohttp.ClientSession], Awaitable[Rates]]] = {
    "binance": lambda service, session: service._binance_like("binance", session),
    "aster": lambda service, session: service._binance_like("aster", session),
    "mexc": _mexc,
    "gate": lambda service, session: service._gate(session),
    "bingx": _bingx,
    "bybit": _bybit,
    "bitget": _bitget,
    "kucoin": _kucoin,
    "hyperliquid": _hyperliquid,
}
