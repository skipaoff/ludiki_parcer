"""
VFP: Binance USDⓈ-M futures behind the adapter contract — contracts, public quotes, clock probe, account and key check.
Changes when: Binance changes these endpoints or the terminal needs more from Binance.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Floats for money — balances and fee rates are parsed from Binance's decimal strings.

Endpoints and response shapes: docs/EXCHANGES.md, section Binance.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms
from app.core.pairs import Quote
from app.core.schemas import Instrument
from app.core.symbols import parse_symbol
from app.exchanges.base import ClockProbe
from app.exchanges.ccxt_support import now_ms, parse, step, to_bool

FEE_REFERENCE_SYMBOL = "BTCUSDT"


def _filters(symbol: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["filterType"]: item for item in symbol.get("filters", [])}


def parse_instruments(exchange_info: dict[str, Any]) -> list[Instrument]:
    """
    GET /fapi/v1/exchangeInfo — USDT-margined perpetuals that are trading now.
    Market orders obey MARKET_LOT_SIZE (falls back to LOT_SIZE); 1000PEPEUSDT quantity and price are per 1000 tokens.
    """
    instruments = []
    for symbol in exchange_info.get("symbols", []):
        if (
            symbol.get("contractType") != "PERPETUAL"
            or symbol.get("quoteAsset") != "USDT"
            or symbol.get("marginAsset", "USDT") != "USDT"
            or symbol.get("status") != "TRADING"
        ):
            continue
        parsed = parse_symbol(symbol["symbol"], "binance")
        if parsed is None:
            continue
        filters = _filters(symbol)
        lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
        step = Decimal(str(lot["stepSize"]))
        if step <= 0:
            step = Decimal(str(filters["LOT_SIZE"]["stepSize"]))
        instruments.append(
            Instrument(
                exchange="binance",
                symbol_raw=symbol["symbol"],
                token=parsed.token,
                qty_unit_tokens=parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=step,
                min_qty_units=Decimal(str(lot["minQty"])),
                max_market_qty_units=Decimal(str(lot["maxQty"])) if "maxQty" in lot else None,
                min_notional_usd=Decimal(str(filters.get("MIN_NOTIONAL", {}).get("notional", "0"))),
                price_tick=Decimal(str(filters["PRICE_FILTER"]["tickSize"])) if "PRICE_FILTER" in filters else None,
            )
        )
    return instruments


def parse_quotes(
    premium_index: list[dict[str, Any]],
    book_ticker: list[dict[str, Any]],
    ticker_24h: list[dict[str, Any]],
) -> dict[str, Quote]:
    """GET /fapi/v1/premiumIndex, /fapi/v1/ticker/bookTicker and /fapi/v1/ticker/24hr, joined by symbol."""
    books = {item["symbol"]: item for item in book_ticker}
    volumes = {item["symbol"]: item for item in ticker_24h}
    quotes = {}
    for item in premium_index:
        symbol = item["symbol"]
        book = books.get(symbol, {})
        volume = volumes.get(symbol, {})
        quotes[symbol] = Quote(
            bid=_positive(book.get("bidPrice")),
            ask=_positive(book.get("askPrice")),
            mark=_positive(item.get("markPrice")),
            index=_positive(item.get("indexPrice")),
            volume24h_usd=_positive(volume.get("quoteVolume")),
        )
    return quotes


def _positive(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    number = Decimal(str(value))
    return number if number > 0 else None


def parse_permissions(raw: dict[str, Any]) -> KeyPermissions:
    """GET /sapi/v1/account/apiRestrictions."""
    return KeyPermissions(
        reading=to_bool(raw.get("enableReading")),
        futures=to_bool(raw.get("enableFutures")),
        withdrawals=to_bool(raw.get("enableWithdrawals")),
        ip_restricted=to_bool(raw.get("ipRestrict")),
    )


def parse_one_way(raw: dict[str, Any]) -> bool | None:
    """GET /fapi/v1/positionSide/dual — dualSidePosition true means hedge mode."""
    dual = to_bool(raw.get("dualSidePosition"))
    return None if dual is None else not dual


def parse_usdt_balance(raw: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    """GET /fapi/v3/balance — wallet balance and available balance of USDT; zero when the asset is absent."""
    for item in raw:
        if item.get("asset") == "USDT":
            return Decimal(str(item["balance"])), Decimal(str(item["availableBalance"]))
    return Decimal(0), Decimal(0)


def parse_fee_rates(raw: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """GET /fapi/v1/commissionRate — rates are fractions ("0.000500"), returned as percent (taker, maker)."""
    return Decimal(str(raw["takerCommissionRate"])) * 100, Decimal(str(raw["makerCommissionRate"])) * 100


class BinanceAdapter:
    name = "binance"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        demo: bool = False,
        timeout_s: float = 10,
    ) -> None:
        self._demo = demo
        self._client = ccxt.binanceusdm(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
            }
        )
        # Probes get their own client: in the shared one ccxt's rate limiter queues them behind heavy catalog
        # requests (ticker/24hr costs 40), which showed up as multi-second pings.
        self._probe_client = ccxt.binanceusdm({"enableRateLimit": False, "timeout": int(timeout_s * 1000)})
        if demo:
            self._client.enable_demo_trading(True)
            self._probe_client.enable_demo_trading(True)

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self._client.fapipublic_get_exchangeinfo())

    async def fetch_quotes(self) -> dict[str, Quote]:
        premium, books, tickers = await asyncio.gather(
            self._client.fapipublic_get_premiumindex(),
            self._client.fapipublic_get_ticker_bookticker(),
            self._client.fapipublic_get_ticker_24hr(),
        )
        return parse_quotes(premium, books, tickers)

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.fapipublic_get_time()
        received = now_ms()
        server = int(response["serverTime"])
        offset = clock_offset_ms(sent, received, server)
        # ccxt signs Binance requests with local time minus timeDifference.
        self._client.options["timeDifference"] = -offset
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=offset, server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())

        if self._demo:
            # Demo trading has no key-restrictions endpoint, and a demo account holds nothing to withdraw.
            permissions = KeyPermissions(reading=True, futures=True, withdrawals=False, ip_restricted=None)
        else:
            raw_permissions = await step(errors, "permissions", self._client.sapi_get_account_apirestrictions())
            permissions = parse(errors, "permissions", parse_permissions, raw_permissions) or KeyPermissions()

        raw_mode = await step(errors, "position_mode", self._client.fapiprivate_get_positionside_dual())
        raw_balance = await step(errors, "balance", self._client.fapiprivatev3_get_balance())
        raw_fees = await step(notes, "fees", self._client.fapiprivate_get_commissionrate({"symbol": FEE_REFERENCE_SYMBOL}))

        wallet, available = parse(errors, "balance", parse_usdt_balance, raw_balance) or (None, None)
        taker, maker = parse(notes, "fees", parse_fee_rates, raw_fees) or (None, None)
        return AccountFacts(
            permissions=permissions,
            one_way_position_mode=parse(errors, "position_mode", parse_one_way, raw_mode),
            wallet_usdt=wallet,
            available_usdt=available,
            taker_fee_pct=taker,
            maker_fee_pct=maker,
            ping_ms=probe.ping_ms if probe else None,
            clock_offset_ms=probe.clock_offset_ms if probe else None,
            errors=tuple(errors),
            notes=tuple(notes),
        )
