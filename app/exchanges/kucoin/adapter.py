"""
VFP: KuCoin Futures USDT-margined perpetuals behind the adapter contract — contracts, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: KuCoin changes its futures API or the terminal needs more from KuCoin.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Reading lots as tokens — sizes are in lots of the contract multiplier (XBTUSDTM: 0.001 BTC a lot); every quantity is
   converted through the instrument.
3. Trusting the submit answer as a result — it carries only the order id, so the order status is read right after.

Keys: API key, secret and the passphrase chosen when the key was made. XBT is BTC.
Endpoints and shapes: docs/EXCHANGES.md, section KuCoin. Public shapes checked live on 15.09.2026, private ones follow the documentation.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
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

EXCHANGE = "kucoin"
PERPETUAL_TYPE = "FFWCSX"
FEE_REFERENCE_SYMBOL = "XBTUSDTM"
STATUS_POLLS = 4
STATUS_POLL_INTERVAL_S = 0.15


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def data_of(raw: Any) -> Any:
    """Answers {"code": "200000", "data": ...}; ccxt raises on other codes."""
    return raw.get("data", raw) if isinstance(raw, dict) else raw


def parse_instruments(contracts: list[dict[str, Any]]) -> list[Instrument]:
    """
    GET /api/v1/contracts/active — USDT-settled perpetuals (type FFWCSX) that are open.
    multiplier is the coins of the symbol in one lot (XBTUSDTM 0.001 BTC, 10000CATUSDTM 10 × 10000 CAT); lotSize is the
    step and minimum in lots; marketMaxOrderQty the market order maximum; prices are per coin of the symbol.
    """
    instruments = []
    for item in contracts:
        symbol = str(item.get("symbol") or "")
        if (
            item.get("type") != PERPETUAL_TYPE
            or item.get("status") != "Open"
            or item.get("quoteCurrency") != "USDT"
            or item.get("settleCurrency") != "USDT"
            or to_bool(item.get("isInverse")) is True
            or not symbol.endswith("USDTM")
        ):
            continue
        parsed = parse_symbol(symbol, EXCHANGE)
        lot_tokens = _positive(item.get("multiplier"))
        lot_size = _positive(item.get("lotSize")) or Decimal(1)
        if parsed is None or lot_tokens is None:
            continue
        instruments.append(
            Instrument(
                exchange=EXCHANGE,
                symbol_raw=symbol,
                token=parsed.token,
                qty_unit_tokens=lot_tokens * parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=lot_size,
                min_qty_units=lot_size,
                max_market_qty_units=_positive(item.get("marketMaxOrderQty")) or _positive(item.get("maxOrderQty")),
                min_notional_usd=Decimal(0),
                price_tick=_positive(item.get("tickSize")),
            )
        )
    return instruments


def parse_quotes(tickers: list[dict[str, Any]], contracts: list[dict[str, Any]]) -> dict[str, Quote]:
    """GET /api/v1/allTickers (bestBidPrice, bestAskPrice) joined with /api/v1/contracts/active (markPrice, indexPrice, turnoverOf24h)."""
    books = {item["symbol"]: item for item in tickers if item.get("symbol")}
    quotes = {}
    for item in contracts:
        symbol = item.get("symbol")
        if not symbol:
            continue
        book = books.get(symbol, {})
        quotes[symbol] = Quote(
            bid=_positive(book.get("bestBidPrice")),
            ask=_positive(book.get("bestAskPrice")),
            mark=_positive(item.get("markPrice")),
            index=_positive(item.get("indexPrice")),
            volume24h_usd=_positive(item.get("turnoverOf24h")),
        )
    return quotes


def parse_positions(raw: list[dict[str, Any]], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    GET /api/v1/positions — currentQty in lots with sign, avgEntryPrice, markPrice, liquidationPrice, realLeverage or
    leverage, marginMode ISOLATED/CROSS (older answers: crossMode), currentTimestamp.
    """
    positions = []
    for item in raw:
        instrument = instruments.get(item.get("symbol", ""))
        lots = _decimal(item.get("currentQty")) or Decimal(0)
        if instrument is None or lots == 0:
            continue
        unit_price = instrument.price_unit_tokens
        mark = _positive(item.get("markPrice"))
        liquidation = _positive(item.get("liquidationPrice"))
        mode = str(item.get("marginMode") or "").upper()
        if not mode and item.get("crossMode") is not None:
            mode = "CROSS" if to_bool(item.get("crossMode")) else "ISOLATED"
        leverage = _decimal(item.get("leverage")) or _decimal(item.get("realLeverage"))
        positions.append(
            Position(
                exchange=EXCHANGE,
                symbol_raw=item["symbol"],
                token=instrument.token,
                side=LegSide.LONG if lots > 0 else LegSide.SHORT,
                qty_tokens=abs(lots) * instrument.qty_unit_tokens,
                entry_price=(_decimal(item.get("avgEntryPrice")) or Decimal(0)) / unit_price,
                mark_price=mark / unit_price if mark else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(leverage) if leverage else None,
                margin_mode="isolated" if mode == "ISOLATED" else "cross" if mode == "CROSS" else None,
                updated_ms=int(item.get("currentTimestamp") or 0),
            )
        )
    return positions


def parse_balance(raw: dict[str, Any]) -> Balance:
    """GET /api/v1/account-overview?currency=USDT — accountEquity, availableBalance, positionMargin + orderMargin."""
    return Balance(
        exchange=EXCHANGE,
        equity_usd=_decimal(raw.get("accountEquity")) or Decimal(0),
        available_usd=_decimal(raw.get("availableBalance")) or Decimal(0),
        margin_used_usd=(_decimal(raw.get("positionMargin")) or Decimal(0)) + (_decimal(raw.get("orderMargin")) or Decimal(0)),
    )


def parse_usdt_balance(raw: dict[str, Any]) -> tuple[Decimal, Decimal]:
    return _decimal(raw.get("marginBalance")) or _decimal(raw.get("accountEquity")) or Decimal(0), _decimal(raw.get("availableBalance")) or Decimal(0)


def parse_funding(raw: Any, since_ms: int) -> Decimal:
    """GET /api/v1/funding-history?symbol= — dataList: funding (positive received, negative paid), timePoint."""
    items = raw.get("dataList") if isinstance(raw, dict) else raw
    return sum(
        ((_decimal(item.get("funding")) or Decimal(0)) for item in items or [] if int(item.get("timePoint") or 0) >= since_ms),
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
    """
    GET /api/v1/orders/byClientOid — status open/done, isActive, cancelExist, filledSize (lots), filledValue (USDT),
    avgDealPrice when present; otherwise the average is filledValue over filled tokens.
    """
    lots = _decimal(raw.get("filledSize")) or _decimal(raw.get("dealSize")) or Decimal(0)
    filled = lots * instrument.qty_unit_tokens
    value = _positive(raw.get("filledValue")) or _positive(raw.get("dealValue"))
    average = _positive(raw.get("avgDealPrice"))
    if average is not None:
        per_token = average / instrument.price_unit_tokens
    else:
        per_token = value / filled if value and filled > 0 else None
    active = to_bool(raw.get("isActive"))
    done = raw.get("status") == "done" or active is False
    if not done:
        outcome = OrderOutcome.UNKNOWN
    elif filled >= requested_tokens:
        outcome = OrderOutcome.FILLED
    elif filled > 0:
        outcome = OrderOutcome.PARTIAL
    else:
        outcome = OrderOutcome.REJECTED
    status = f"{raw.get('status')}{':canceled' if to_bool(raw.get('cancelExist')) else ''}"
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(raw["id"]) if raw.get("id") else None,
        outcome=outcome,
        status=status,
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=per_token,
        fee_usd=None,
        error_code=None if outcome is not OrderOutcome.REJECTED else status,
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=raw,
    )


def parse_fills(raw: Any, instrument: Instrument) -> list[FillReport]:
    """GET /api/v1/fills?orderId= — items: tradeId, price, size (lots), fee, feeCurrency, liquidity maker/taker, tradeTime (ns)."""
    items = raw.get("items") if isinstance(raw, dict) else raw
    return [
        FillReport(
            exchange_fill_id=str(item["tradeId"]),
            ts_ms=int(item.get("tradeTime") or 0) // 1_000_000 if int(item.get("tradeTime") or 0) > 10**15 else int(item.get("tradeTime") or item.get("createdAt") or 0),
            price=(_decimal(item.get("price")) or Decimal(0)) / instrument.price_unit_tokens,
            qty_tokens=(_decimal(item.get("size")) or Decimal(0)) * instrument.qty_unit_tokens,
            fee=abs(_decimal(item.get("fee")) or Decimal(0)),
            fee_asset=str(item.get("feeCurrency") or "USDT"),
            is_maker=None if item.get("liquidity") is None else item.get("liquidity") == "maker",
        )
        for item in items or []
        if item.get("tradeId")
    ]


def parse_permissions(raw: dict[str, Any]) -> KeyPermissions:
    """GET /api/v1/user/api-key — permission "General,Futures,Spot,Withdrawal", ipWhitelist."""
    granted = {part.strip() for part in str(raw.get("permission") or "").split(",") if part.strip()}
    if not granted:
        return KeyPermissions()
    return KeyPermissions(
        reading="General" in granted,
        futures="Futures" in granted,
        withdrawals="Withdrawal" in granted,
        ip_restricted=bool(raw.get("ipWhitelist")) if "ipWhitelist" in raw else None,
    )


def parse_one_way(raw: Any) -> bool | None:
    """GET /api/v2/position/getPositionMode — positionMode "0" one-way, "1" hedge (as in Switch Position Mode)."""
    mode = str(raw.get("positionMode")) if isinstance(raw, dict) and raw.get("positionMode") is not None else None
    return None if mode not in ("0", "1") else mode == "0"


def parse_fee_rates(raw: Any) -> tuple[Decimal, Decimal]:
    """GET /api/v1/trade-fees?symbols= — takerFeeRate, makerFeeRate as fractions; returned as percent."""
    item = raw[0] if isinstance(raw, list) else raw
    return Decimal(str(item["takerFeeRate"])) * 100, Decimal(str(item["makerFeeRate"])) * 100


def _not_found(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, ccxt.OrderNotFound) or "not exist" in text or "100001" in text


class KucoinAdapter:
    name = EXCHANGE

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, passphrase: str | None = None, timeout_s: float = 10) -> None:
        config = {"apiKey": api_key or "", "secret": api_secret or "", "password": passphrase or "", "enableRateLimit": True,
                  "timeout": int(timeout_s * 1000)}
        self._client = with_shared_context(ccxt.kucoinfutures(config))
        self._probe_client = with_shared_context(ccxt.kucoinfutures({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(data_of(await self._client.futurespublic_get_contracts_active()) or [])

    async def fetch_quotes(self) -> dict[str, Quote]:
        tickers, contracts = await asyncio.gather(
            self._client.futurespublic_get_alltickers(), self._client.futurespublic_get_contracts_active()
        )
        return parse_quotes(data_of(tickers) or [], data_of(contracts) or [])

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.futurespublic_get_timestamp()
        received = now_ms()
        server = int(data_of(response))
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server), server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        raw_permissions = await step(errors, "permissions", self._client.private_get_user_api_key())
        raw_mode = await step(errors, "position_mode", self._client.futuresprivate_get_position_getpositionmode())
        raw_balance = await step(errors, "balance", self._client.futuresprivate_get_account_overview({"currency": "USDT"}))
        raw_fees = await step(notes, "fees", self._client.futuresprivate_get_trade_fees({"symbols": FEE_REFERENCE_SYMBOL}))
        permissions = parse(errors, "permissions", lambda raw: parse_permissions(data_of(raw)), raw_permissions) or KeyPermissions()
        wallet, available = parse(errors, "balance", lambda raw: parse_usdt_balance(data_of(raw)), raw_balance) or (None, None)
        taker, maker = parse(notes, "fees", lambda raw: parse_fee_rates(data_of(raw)), raw_fees) or (None, None)
        return AccountFacts(
            permissions=permissions,
            one_way_position_mode=parse(errors, "position_mode", lambda raw: parse_one_way(data_of(raw)), raw_mode),
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
        return parse_positions(data_of(await self._client.futuresprivate_get_positions({"currency": "USDT"})) or [], instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(data_of(await self._client.futuresprivate_get_account_overview({"currency": "USDT"})))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raw = data_of(await self._client.futuresprivate_get_funding_history({"symbol": symbol_raw, "startAt": since_ms, "maxCount": 200}))
        return parse_funding(raw, since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        await self._client.futuresprivate_post_position_changemarginmode(
            {"symbol": instrument.symbol_raw, "marginMode": "ISOLATED" if isolated else "CROSS"}
        )
        if not isolated:
            # Isolated leverage travels with each order; cross leverage is a setting of the symbol.
            await self._client.futuresprivate_post_changecrossuserleverage({"symbol": instrument.symbol_raw, "leverage": str(leverage)})

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
            "clientOid": client_order_id,
            "side": "buy" if (leg is LegSide.LONG) == opening else "sell",
            "symbol": instrument.symbol_raw,
            "type": "market",
            "size": int(qty_units),
            "leverage": leverage,
            "marginMode": "ISOLATED" if isolated else "CROSS",
        }
        if not opening:
            params["reduceOnly"] = True
        sent = int(now_ms())
        try:
            data = data_of(await self._client.futuresprivate_post_orders(params))
        except Exception as exc:
            unknown = is_unknown_outcome_error(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN if unknown else OrderOutcome.REJECTED, "error", requested,
                Decimal(0), None, None, error_code(exc), describe_error("order", exc), sent, None if unknown else int(now_ms()),
            )
        ack = int(now_ms())
        order_id = str(data.get("orderId")) if isinstance(data, dict) and data.get("orderId") else None
        report = OrderReport(client_order_id, order_id, OrderOutcome.UNKNOWN, "submitted", requested, Decimal(0), None, None, None, None, sent, ack)
        for _ in range(STATUS_POLLS):
            await asyncio.sleep(STATUS_POLL_INTERVAL_S)
            status = await self.fetch_order(instrument, client_order_id, requested)
            if status.outcome is OrderOutcome.REJECTED and status.status == "not_found":
                continue
            report = status
            if status.outcome is not OrderOutcome.UNKNOWN:
                break
        return replace(report, sent_ts_ms=sent, ack_ts_ms=ack, exchange_order_id=report.exchange_order_id or order_id)

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        sent = int(now_ms())
        try:
            data = data_of(await self._client.futuresprivate_get_orders_byclientoid({"clientOid": client_order_id}))
        except Exception as exc:
            missing = _not_found(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.REJECTED if missing else OrderOutcome.UNKNOWN, "not_found" if missing else "error",
                requested_tokens, Decimal(0), None, None, error_code(exc), describe_error("order_status", exc), sent, None,
            )
        if not isinstance(data, dict) or not data:
            return OrderReport(
                client_order_id, None, OrderOutcome.REJECTED, "not_found", requested_tokens, Decimal(0), None, None,
                "not_found", "no order with this client id", sent, None,
            )
        return parse_order(data, instrument, client_order_id, requested_tokens, sent, int(now_ms()))

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        return parse_fills(data_of(await self._client.futuresprivate_get_fills({"orderId": report.exchange_order_id})), instrument)
