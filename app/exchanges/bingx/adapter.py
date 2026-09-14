"""
VFP: BingX USDT perpetual swap behind the adapter contract — contracts, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: BingX changes its swap API or the terminal needs more from BingX.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Trusting the submit answer as a result — it carries only the order id, so the order status is read right after.
3. Orders marked as a broker's — ccxt tags requests with its broker id, which BingX may restrict per pair; the tag is removed.
4. Fill quantities read in the wrong unit — fills are requested in coins and checked against the order's filled quantity.

Contract quantities are in coins of the contract (1000PEPE-USDT: lots of 1000 PEPE), prices per coin of the contract.
Endpoints and shapes: docs/EXCHANGES.md, section BingX. Public shapes checked live on 14.09.2026, private ones follow the documentation.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
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

EXCHANGE = "bingx"
ORDER_SIDES = {(LegSide.LONG, True): "BUY", (LegSide.SHORT, True): "SELL", (LegSide.LONG, False): "SELL", (LegSide.SHORT, False): "BUY"}
FINAL_WITHOUT_FULL_FILL = {"CANCELLED", "CANCELED", "FAILED", "EXPIRED", "REJECTED"}
PERMISSION_READ, PERMISSION_PERPETUAL, PERMISSION_WITHDRAW = 2, 3, 5
NOT_FOUND_CODES = ("80016", "80017", "100400")
STATUS_POLLS = 4
STATUS_POLL_INTERVAL_S = 0.15
FILLS_LOOKBACK_MS = 60_000


class _Client(ccxt.bingx):
    def sign(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = super().sign(*args, **kwargs)
        headers = request.get("headers")
        if isinstance(headers, dict):
            headers.pop("X-SOURCE-KEY", None)
        return request


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def data_of(raw: Any) -> Any:
    """Swap API answers {"code": 0, "msg": "", "data": ...}; ccxt raises on other codes. Some account answers come bare."""
    return raw.get("data", raw) if isinstance(raw, dict) else raw


def units_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_instruments(contracts: list[dict[str, Any]]) -> list[Instrument]:
    """
    GET /openApi/swap/v2/quote/contracts — USDT perpetuals online (status 1) that the API may open and close.
    quantityPrecision is the quantity step in coins; tradeMinQuantity (coins) and tradeMinUSDT are the order minimums.
    """
    instruments = []
    for item in contracts:
        symbol = str(item.get("symbol") or "")
        if (
            item.get("currency") != "USDT"
            or item.get("status") != 1
            or to_bool(item.get("apiStateOpen")) is not True
            or to_bool(item.get("apiStateClose")) is not True
            or not symbol.endswith("-USDT")
        ):
            continue
        parsed = parse_symbol(symbol, EXCHANGE)
        if parsed is None or item.get("quantityPrecision") is None:
            continue
        price_precision = item.get("pricePrecision")
        instruments.append(
            Instrument(
                exchange=EXCHANGE,
                symbol_raw=symbol,
                token=parsed.token,
                qty_unit_tokens=parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=Decimal(1).scaleb(-int(item["quantityPrecision"])),
                min_qty_units=_decimal(item.get("tradeMinQuantity")) or Decimal(0),
                max_market_qty_units=None,
                min_notional_usd=_decimal(item.get("tradeMinUSDT")) or Decimal(0),
                price_tick=Decimal(1).scaleb(-int(price_precision)) if price_precision is not None else None,
            )
        )
    return instruments


def parse_quotes(tickers: list[dict[str, Any]], premium: list[dict[str, Any]]) -> dict[str, Quote]:
    """GET /openApi/swap/v2/quote/ticker (bidPrice, askPrice, quoteVolume) and /premiumIndex (markPrice, indexPrice), by symbol."""
    marks = {item["symbol"]: item for item in premium if item.get("symbol")}
    quotes = {}
    for item in tickers:
        symbol = item.get("symbol")
        if not symbol:
            continue
        mark = marks.get(symbol, {})
        quotes[symbol] = Quote(
            bid=_positive(item.get("bidPrice")),
            ask=_positive(item.get("askPrice")),
            mark=_positive(mark.get("markPrice")),
            index=_positive(mark.get("indexPrice")),
            volume24h_usd=_positive(item.get("quoteVolume")),
        )
    return quotes


def parse_positions(raw: list[dict[str, Any]], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    GET /openApi/swap/v2/user/positions — positionSide LONG or SHORT (BOTH falls back to the sign of positionAmt),
    positionAmt in coins, avgPrice the entry, isolated true or false, liquidationPrice 0 when there is none.
    """
    positions = []
    for item in raw:
        instrument = instruments.get(item.get("symbol", ""))
        amount = _decimal(item.get("positionAmt")) or Decimal(0)
        if instrument is None or amount == 0:
            continue
        side_text = str(item.get("positionSide") or "").upper()
        if side_text in ("LONG", "SHORT"):
            side = LegSide.LONG if side_text == "LONG" else LegSide.SHORT
        else:
            side = LegSide.LONG if amount > 0 else LegSide.SHORT
        unit_price = instrument.price_unit_tokens
        mark = _positive(item.get("markPrice"))
        liquidation = _positive(item.get("liquidationPrice"))
        isolated = to_bool(item.get("isolated"))
        positions.append(
            Position(
                exchange=EXCHANGE,
                symbol_raw=item["symbol"],
                token=instrument.token,
                side=side,
                qty_tokens=abs(amount) * instrument.qty_unit_tokens,
                entry_price=(_decimal(item.get("avgPrice")) or Decimal(0)) / unit_price,
                mark_price=mark / unit_price if mark else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(item["leverage"]) if item.get("leverage") else None,
                margin_mode=None if isolated is None else ("isolated" if isolated else "cross"),
                updated_ms=int(item.get("updateTime") or 0),
            )
        )
    return positions


def _usdt_asset(raw: Any) -> dict[str, Any]:
    items = raw if isinstance(raw, list) else [raw]
    return next((item for item in items if isinstance(item, dict) and item.get("asset") == "USDT"), {})


def parse_balance(raw: Any) -> Balance:
    """GET /openApi/swap/v3/user/balance — the USDT entry: equity, availableMargin, usedMargin and freezedMargin (orders)."""
    asset = _usdt_asset(raw)
    return Balance(
        exchange=EXCHANGE,
        equity_usd=_decimal(asset.get("equity")) or Decimal(0),
        available_usd=_decimal(asset.get("availableMargin")) or Decimal(0),
        margin_used_usd=(_decimal(asset.get("usedMargin")) or Decimal(0)) + (_decimal(asset.get("freezedMargin")) or Decimal(0)),
    )


def parse_usdt_balance(raw: Any) -> tuple[Decimal, Decimal]:
    asset = _usdt_asset(raw)
    return _decimal(asset.get("balance")) or Decimal(0), _decimal(asset.get("availableMargin")) or Decimal(0)


def parse_funding(raw: list[dict[str, Any]], since_ms: int) -> Decimal:
    """GET /openApi/swap/v2/user/income?incomeType=FUNDING_FEE — positive income was received, negative was paid."""
    return sum(
        (
            _decimal(item.get("income")) or Decimal(0)
            for item in raw
            if item.get("incomeType") == "FUNDING_FEE" and int(item.get("time") or 0) >= since_ms
        ),
        Decimal(0),
    )


def parse_order(
    raw: dict[str, Any],
    instrument: Instrument,
    client_order_id: str,
    requested_tokens: Decimal,
    sent_ms: int,
    ack_ms: int | None,
) -> OrderReport:
    """GET /openApi/swap/v2/trade/order — data.order: status NEW/PENDING/PARTIALLY_FILLED/FILLED/CANCELLED/FAILED, executedQty in coins, avgPrice."""
    status = str(raw.get("status") or "").upper()
    filled = (_decimal(raw.get("executedQty")) or Decimal(0)) * instrument.qty_unit_tokens
    average = _positive(raw.get("avgPrice"))
    if status == "FILLED":
        outcome = OrderOutcome.FILLED if filled >= requested_tokens else OrderOutcome.PARTIAL
    elif status in FINAL_WITHOUT_FULL_FILL:
        outcome = OrderOutcome.PARTIAL if filled > 0 else OrderOutcome.REJECTED
    else:
        outcome = OrderOutcome.UNKNOWN
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(raw["orderId"]) if raw.get("orderId") not in (None, "", 0) else None,
        outcome=outcome,
        status=status,
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=average / instrument.price_unit_tokens if average else None,
        fee_usd=None,
        error_code=None if outcome is not OrderOutcome.REJECTED else status.lower() or "not_filled",
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=raw,
    )


def _iso_ms(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def parse_fills(raw: list[dict[str, Any]], instrument: Instrument, expected_units: Decimal | None = None) -> list[FillReport]:
    """
    GET /openApi/swap/v2/trade/allFillOrders?tradingUnit=COIN — fills carry no id of their own, so one is built from the
    order id, time and position in a stable order. Should the volumes add up to the order's filled quantity in tokens
    rather than in contract coins, they are converted back.
    """
    rows = sorted(raw, key=lambda item: (str(item.get("filledTm") or ""), str(item.get("price")), str(item.get("volume"))))
    volumes = [_decimal(item.get("volume")) or Decimal(0) for item in rows]
    divisor = Decimal(1)
    multiplier = instrument.qty_unit_tokens
    if expected_units and multiplier != 1 and sum(volumes) == expected_units * multiplier:
        divisor = multiplier
    fills = []
    for index, (item, volume) in enumerate(zip(rows, volumes)):
        ts = _iso_ms(item.get("filledTm"))
        fills.append(
            FillReport(
                exchange_fill_id=f"{item.get('orderId')}-{ts}-{index}",
                ts_ms=ts,
                price=(_decimal(item.get("price")) or Decimal(0)) / instrument.price_unit_tokens,
                qty_tokens=volume / divisor * multiplier,
                fee=abs(_decimal(item.get("commission")) or Decimal(0)),
                fee_asset=str(item.get("currency") or "USDT"),
                is_maker=None,
            )
        )
    return fills


def parse_permissions(raw: Any) -> KeyPermissions:
    """GET /openApi/v1/account/apiPermissions — permissions: 1 spot, 2 read, 3 perpetual futures, 4 transfer, 5 withdraw; ipAddresses."""
    data = data_of(raw)
    codes = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(codes, list):
        return KeyPermissions()
    granted = {int(code) for code in codes}
    addresses = data.get("ipAddresses")
    return KeyPermissions(
        reading=PERMISSION_READ in granted,
        futures=PERMISSION_PERPETUAL in granted,
        withdrawals=PERMISSION_WITHDRAW in granted,
        ip_restricted=bool(addresses) if isinstance(addresses, list) else None,
    )


def parse_one_way(raw: Any) -> bool | None:
    """GET /openApi/swap/v1/positionSide/dual — dualSidePosition "true" means hedge mode."""
    dual = to_bool(data_of(raw).get("dualSidePosition"))
    return None if dual is None else not dual


def parse_fee_rates(raw: Any) -> tuple[Decimal, Decimal]:
    """GET /openApi/swap/v2/user/commissionRate — data.commission rates as fractions, returned as percent (taker, maker)."""
    commission = data_of(raw)["commission"]
    return Decimal(str(commission["takerCommissionRate"])) * 100, Decimal(str(commission["makerCommissionRate"])) * 100


def submitted_order_id(raw: Any) -> str | None:
    """POST /openApi/swap/v2/trade/order — data.order.orderId."""
    data = data_of(raw)
    order = data.get("order", data) if isinstance(data, dict) else {}
    value = order.get("orderId") if isinstance(order, dict) else None
    return None if value in (None, "", 0) else str(value)


def _not_found(exc: BaseException) -> bool:
    text = str(exc)
    return isinstance(exc, ccxt.OrderNotFound) or "not exist" in text.lower() or any(code in text for code in NOT_FOUND_CODES)


class BingxAdapter:
    name = EXCHANGE

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, timeout_s: float = 10) -> None:
        self._client = _Client(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
                "options": {"defaultType": "swap"},
            }
        )
        # Probes get their own client so the rate limiter never queues them behind catalog requests.
        self._probe_client = _Client({"enableRateLimit": False, "timeout": int(timeout_s * 1000)})

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(data_of(await self._client.swap_v2_public_get_quote_contracts()))

    async def fetch_quotes(self) -> dict[str, Quote]:
        tickers, premium = await asyncio.gather(
            self._client.swap_v2_public_get_quote_ticker(),
            self._client.swap_v2_public_get_quote_premiumindex(),
        )
        return parse_quotes(data_of(tickers), data_of(premium))

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.swap_v2_public_get_server_time()
        received = now_ms()
        server = int(data_of(response)["serverTime"])
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server), server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        raw_permissions = await step(errors, "permissions", self._client.account_v1_private_get_account_apipermissions())
        raw_mode = await step(errors, "position_mode", self._client.swap_v1_private_get_positionside_dual())
        raw_balance = await step(errors, "balance", self._client.swap_v3_private_get_user_balance())
        raw_fees = await step(notes, "fees", self._client.swap_v2_private_get_user_commissionrate())
        permissions = parse(errors, "permissions", parse_permissions, raw_permissions) or KeyPermissions()
        wallet, available = parse(errors, "balance", lambda raw: parse_usdt_balance(data_of(raw)), raw_balance) or (None, None)
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

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        return parse_positions(data_of(await self._client.swap_v2_private_get_user_positions()) or [], instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(data_of(await self._client.swap_v3_private_get_user_balance()))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raw = await self._client.swap_v2_private_get_user_income(
            {"symbol": symbol_raw, "incomeType": "FUNDING_FEE", "startTime": since_ms, "limit": 1000}
        )
        return parse_funding(data_of(raw) or [], since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        wanted = "ISOLATED" if isolated else "CROSSED"
        current = data_of(await self._client.swap_v2_private_get_trade_margintype({"symbol": instrument.symbol_raw}))
        if not isinstance(current, dict) or current.get("marginType") != wanted:
            await self._client.swap_v2_private_post_trade_margintype({"symbol": instrument.symbol_raw, "marginType": wanted})
        # One-way mode accepts only side BOTH.
        await self._client.swap_v2_private_post_trade_leverage({"symbol": instrument.symbol_raw, "side": "BOTH", "leverage": leverage})

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
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": units_text(qty_units),
            "clientOrderID": client_order_id,
        }
        if not opening:
            params["reduceOnly"] = "true"
        sent = int(now_ms())
        try:
            order_id = submitted_order_id(await self._client.swap_v2_private_post_trade_order(params))
        except Exception as exc:
            unknown = is_unknown_outcome_error(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN if unknown else OrderOutcome.REJECTED, "error", requested,
                Decimal(0), None, None, error_code(exc), describe_error("order", exc), sent, None if unknown else int(now_ms()),
            )
        ack = int(now_ms())
        # The submit answer carries only the id; a market order settles within moments.
        report = OrderReport(client_order_id, order_id, OrderOutcome.UNKNOWN, "submitted", requested, Decimal(0), None, None, None, None, sent, ack)
        for _ in range(STATUS_POLLS):
            await asyncio.sleep(STATUS_POLL_INTERVAL_S)
            status = await self.fetch_order(instrument, client_order_id, requested)
            if status.outcome is OrderOutcome.REJECTED and status.status == "not_found":
                continue  # accepted a moment ago; the status endpoint may lag behind the submit
            report = status
            if status.outcome is not OrderOutcome.UNKNOWN:
                break
        return replace(report, sent_ts_ms=sent, ack_ts_ms=ack, exchange_order_id=report.exchange_order_id or order_id)

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        sent = int(now_ms())
        try:
            raw = await self._client.swap_v2_private_get_trade_order({"symbol": instrument.symbol_raw, "clientOrderId": client_order_id})
        except Exception as exc:
            missing = _not_found(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.REJECTED if missing else OrderOutcome.UNKNOWN, "not_found" if missing else "error",
                requested_tokens, Decimal(0), None, None, error_code(exc), describe_error("order_status", exc), sent, None,
            )
        data = data_of(raw)
        order = data.get("order") if isinstance(data, dict) else None
        if not isinstance(order, dict) or not order:
            return OrderReport(client_order_id, None, OrderOutcome.UNKNOWN, "empty", requested_tokens, Decimal(0), None, None, None, None, sent, None)
        return parse_order(order, instrument, client_order_id, requested_tokens, sent, int(now_ms()))

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        now = int(now_ms())
        raw = await self._client.swap_v2_private_get_trade_allfillorders(
            {
                "symbol": instrument.symbol_raw,
                "orderId": report.exchange_order_id,
                "tradingUnit": "COIN",
                "startTs": (report.sent_ts_ms or now) - FILLS_LOOKBACK_MS,
                "endTs": now,
            }
        )
        data = data_of(raw)
        rows = data.get("fill_orders") if isinstance(data, dict) else None
        expected = report.filled_tokens / instrument.qty_unit_tokens if report.filled_tokens else None
        return parse_fills(rows or [], instrument, expected)
