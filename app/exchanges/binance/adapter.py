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
from typing import Any, Mapping

import ccxt.async_support as ccxt

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms
from app.core.legs import OrderOutcome
from app.core.pairs import Quote
from app.core.schemas import Instrument, LegSide
from app.core.symbols import parse_symbol
from app.exchanges.base import Balance, ClockProbe, FillReport, OrderReport, Position
from app.exchanges.ccxt_support import describe_error, error_code, is_unknown_outcome_error, now_ms, parse, step, to_bool
from app.system.tls import with_shared_context

FEE_REFERENCE_SYMBOL = "BTCUSDT"


def _filters(symbol: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["filterType"]: item for item in symbol.get("filters", [])}


def parse_instruments(exchange_info: dict[str, Any], exchange: str = "binance") -> list[Instrument]:
    """
    GET /fapi/v1/exchangeInfo — USDT-margined perpetuals that are trading now.
    Market orders obey MARKET_LOT_SIZE (falls back to LOT_SIZE); 1000PEPEUSDT quantity and price are per 1000 tokens.
    Aster answers in the same shape, hence the exchange argument.
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
        parsed = parse_symbol(symbol["symbol"], exchange)
        if parsed is None:
            continue
        filters = _filters(symbol)
        lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
        step = Decimal(str(lot["stepSize"]))
        if step <= 0:
            step = Decimal(str(filters["LOT_SIZE"]["stepSize"]))
        instruments.append(
            Instrument(
                exchange=exchange,
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


def parse_positions(raw: list[dict[str, Any]], instruments: Mapping[str, Instrument], exchange: str = "binance") -> list[Position]:
    """
    GET /fapi/v2/positionRisk — one-way mode rows (positionSide BOTH); the sign of positionAmt is the side.
    positionAmt is in contract units (1000PEPEUSDT: lots of 1000 PEPE) and prices are per unit; converted per token.
    """
    positions = []
    for item in raw:
        amount = Decimal(str(item.get("positionAmt") or "0"))
        instrument = instruments.get(item.get("symbol", ""))
        if amount == 0 or instrument is None:
            continue
        unit_price = instrument.price_unit_tokens
        liquidation = _positive(item.get("liquidationPrice"))
        mark = _positive(item.get("markPrice"))
        margin = str(item.get("marginType") or "").lower() or None
        positions.append(
            Position(
                exchange=exchange,
                symbol_raw=item["symbol"],
                token=instrument.token,
                side=LegSide.LONG if amount > 0 else LegSide.SHORT,
                qty_tokens=abs(amount) * instrument.qty_unit_tokens,
                entry_price=Decimal(str(item["entryPrice"])) / unit_price,
                mark_price=mark / unit_price if mark else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(item["leverage"]) if item.get("leverage") else None,
                margin_mode=margin if margin in ("isolated", "cross") else None,
                updated_ms=int(item.get("updateTime") or 0),
            )
        )
    return positions


def parse_balance(raw: dict[str, Any], exchange: str = "binance") -> Balance:
    """GET /fapi/v3/account — margin balance (wallet plus unrealized PnL), available balance and initial margin in use."""
    return Balance(
        exchange=exchange,
        equity_usd=Decimal(str(raw["totalMarginBalance"])),
        available_usd=Decimal(str(raw["availableBalance"])),
        margin_used_usd=Decimal(str(raw.get("totalInitialMargin") or "0")),
    )


def parse_funding(raw: list[dict[str, Any]], since_ms: int) -> Decimal:
    """GET /fapi/v1/income?incomeType=FUNDING_FEE — positive income was received, negative was paid."""
    return sum(
        (
            Decimal(str(item["income"]))
            for item in raw
            if item.get("incomeType") == "FUNDING_FEE" and int(item.get("time") or 0) >= since_ms
        ),
        Decimal(0),
    )


ORDER_SIDES = {(LegSide.LONG, True): "BUY", (LegSide.SHORT, True): "SELL", (LegSide.LONG, False): "SELL", (LegSide.SHORT, False): "BUY"}
FINAL_WITHOUT_FULL_FILL = {"EXPIRED", "CANCELED", "REJECTED", "EXPIRED_IN_MATCH"}


def units_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_order(
    raw: dict[str, Any],
    instrument: Instrument,
    client_order_id: str,
    requested_tokens: Decimal,
    sent_ms: int,
    ack_ms: int | None,
) -> OrderReport:
    """POST /fapi/v1/order with newOrderRespType=RESULT, or GET /fapi/v1/order: status, executedQty, avgPrice."""
    status = str(raw.get("status") or "")
    filled = Decimal(str(raw.get("executedQty") or "0")) * instrument.qty_unit_tokens
    average = _positive(raw.get("avgPrice"))
    if status == "FILLED":
        outcome = OrderOutcome.FILLED if filled >= requested_tokens else OrderOutcome.PARTIAL
    elif status in FINAL_WITHOUT_FULL_FILL:
        outcome = OrderOutcome.PARTIAL if filled > 0 else OrderOutcome.REJECTED
    else:
        # NEW or PARTIALLY_FILLED: a market order still working has no final result yet.
        outcome = OrderOutcome.UNKNOWN
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(raw["orderId"]) if raw.get("orderId") is not None else None,
        outcome=outcome,
        status=status,
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=average / instrument.price_unit_tokens if average else None,
        fee_usd=None,
        error_code=None,
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=raw,
    )


def parse_fills(raw: list[dict[str, Any]], instrument: Instrument) -> list[FillReport]:
    """GET /fapi/v1/userTrades?orderId= — commission may be reported negative; stored as a positive cost."""
    return [
        FillReport(
            exchange_fill_id=str(item["id"]),
            ts_ms=int(item["time"]),
            price=Decimal(str(item["price"])) / instrument.price_unit_tokens,
            qty_tokens=Decimal(str(item["qty"])) * instrument.qty_unit_tokens,
            fee=abs(Decimal(str(item.get("commission") or "0"))),
            fee_asset=str(item.get("commissionAsset") or ""),
            is_maker=to_bool(item.get("maker")),
        )
        for item in raw
    ]


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
        self._client = with_shared_context(ccxt.binanceusdm(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
            }
        ))
        # Probes get their own client: in the shared one ccxt's rate limiter queues them behind heavy catalog
        # requests (ticker/24hr costs 40), which showed up as multi-second pings.
        self._probe_client = with_shared_context(ccxt.binanceusdm({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))
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

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        return parse_positions(await self._client.fapiprivatev2_get_positionrisk(), instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(await self._client.fapiprivatev3_get_account())

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raw = await self._client.fapiprivate_get_income(
            {"symbol": symbol_raw, "incomeType": "FUNDING_FEE", "startTime": since_ms, "limit": 1000}
        )
        return parse_funding(raw, since_ms)

    async def create_listen_key(self) -> str:
        """POST /fapi/v1/listenKey — the user data stream address, valid 60 minutes unless kept alive."""
        return (await self._client.fapiprivate_post_listenkey())["listenKey"]

    async def keepalive_listen_key(self) -> None:
        await self._client.fapiprivate_put_listenkey()

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        await self._client.fapiprivate_post_leverage({"symbol": instrument.symbol_raw, "leverage": leverage})
        try:
            await self._client.fapiprivate_post_margintype(
                {"symbol": instrument.symbol_raw, "marginType": "ISOLATED" if isolated else "CROSSED"}
            )
        except Exception as exc:
            # -4046: "No need to change margin type" — it already is what we want.
            if "-4046" not in str(exc):
                raise

    async def place_market_order(
        self,
        instrument: Instrument,
        leg: LegSide,
        opening: bool,
        qty_units: Decimal,
        client_order_id: str,
        leverage: int,
        isolated: bool,
    ) -> OrderReport:
        requested = qty_units * instrument.qty_unit_tokens
        params = {
            "symbol": instrument.symbol_raw,
            "side": ORDER_SIDES[(leg, opening)],
            "type": "MARKET",
            "quantity": units_text(qty_units),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if not opening:
            params["reduceOnly"] = "true"
        sent = int(now_ms())
        try:
            raw = await self._client.fapiprivate_post_order(params)
        except Exception as exc:
            unknown = is_unknown_outcome_error(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN if unknown else OrderOutcome.REJECTED, "error", requested,
                Decimal(0), None, None, error_code(exc), describe_error("order", exc), sent, None if unknown else int(now_ms()),
            )
        return parse_order(raw, instrument, client_order_id, requested, sent, int(now_ms()))

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        sent = int(now_ms())
        try:
            raw = await self._client.fapiprivate_get_order({"symbol": instrument.symbol_raw, "origClientOrderId": client_order_id})
        except Exception as exc:
            # -2013 "Order does not exist": the exchange never accepted it.
            missing = "-2013" in str(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.REJECTED if missing else OrderOutcome.UNKNOWN, "not_found" if missing else "error",
                requested_tokens, Decimal(0), None, None, error_code(exc), describe_error("order_status", exc), sent, None,
            )
        return parse_order(raw, instrument, client_order_id, requested_tokens, sent, int(now_ms()))

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        raw = await self._client.fapiprivate_get_usertrades({"symbol": instrument.symbol_raw, "orderId": report.exchange_order_id})
        return parse_fills(raw, instrument)

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
