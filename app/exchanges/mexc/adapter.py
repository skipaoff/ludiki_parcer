"""
VFP: MEXC futures behind the adapter contract — contracts, public quotes, clock probe, account and key check.
Changes when: MEXC changes its contract API or the terminal needs more from MEXC.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Guessing key permissions — MEXC does not report them for futures keys, so they stay unknown.

Endpoints and response shapes: docs/EXCHANGES.md, section MEXC. ccxt 4.5.78 uses https://api.mexc.com/api/v1.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any, Mapping

import ccxt.async_support as ccxt

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms
from app.core.pairs import Quote
from app.core.schemas import Instrument, LegSide
from app.core.symbols import parse_symbol
from app.core.legs import OrderOutcome
from app.exchanges.base import Balance, ClockProbe, FillReport, OrderReport, Position
from app.exchanges.ccxt_support import describe_error, error_code, is_unknown_outcome_error, now_ms, parse, step
from app.system.tls import with_shared_context

FEE_REFERENCE_SYMBOL = "BTC_USDT"
POSITION_MODES = {1: False, 2: True}  # 1 hedge, 2 one-way


class MexcResponseError(RuntimeError):
    pass


def unwrap(raw: dict[str, Any]) -> Any:
    """Contract API wraps every answer as {"success", "code", "data"}."""
    if not raw.get("success", False):
        raise MexcResponseError(f"code {raw.get('code')}: {raw.get('message') or raw.get('msg') or 'no message'}")
    return raw.get("data")


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def parse_instruments(raw: dict[str, Any]) -> list[Instrument]:
    """
    GET /api/v1/contract/detail — USDT perpetuals that are open and allowed for API trading.
    Quantity is in contracts: one contract holds contractSize base units, and a base unit of 1000BONK is 1000 BONK.
    MEXC has no minimum notional.
    """
    instruments = []
    for item in unwrap(raw) or []:
        if (
            item.get("futureType") != 1
            or item.get("quoteCoin") != "USDT"
            or item.get("settleCoin") != "USDT"
            or item.get("state") != 0
            or not item.get("apiAllowed", False)
            or item.get("isHidden", False)
        ):
            continue
        parsed = parse_symbol(item["symbol"], "mexc")
        if parsed is None:
            continue
        instruments.append(
            Instrument(
                exchange="mexc",
                symbol_raw=item["symbol"],
                token=parsed.token,
                qty_unit_tokens=Decimal(str(item["contractSize"])) * parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=Decimal(str(item.get("volUnit") or 1)),
                min_qty_units=Decimal(str(item["minVol"])),
                max_market_qty_units=_decimal(item.get("maxVol")),
                min_notional_usd=Decimal(0),
                price_tick=_decimal(item.get("priceUnit")),
            )
        )
    return instruments


def parse_quotes(raw: dict[str, Any]) -> dict[str, Quote]:
    """
    GET /api/v1/contract/ticker — all symbols. Best prices are bid1/ask1; maxBidPrice/minAskPrice are price limits,
    not the book. amount24 is the 24h turnover in USDT.
    """
    quotes = {}
    for item in unwrap(raw) or []:
        quotes[item["symbol"]] = Quote(
            bid=_positive(item.get("bid1")),
            ask=_positive(item.get("ask1")),
            mark=_positive(item.get("fairPrice")),
            index=_positive(item.get("indexPrice")),
            volume24h_usd=_positive(item.get("amount24")),
        )
    return quotes


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


POSITION_SIDES = {1: LegSide.LONG, 2: LegSide.SHORT}
MARGIN_MODES = {1: "isolated", 2: "cross"}


def parse_positions(raw: dict[str, Any], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    GET /api/v1/private/position/open_positions — holdVol in contracts, positionType 1 long / 2 short,
    openType 1 isolated / 2 cross, prices per base unit (1000BONK_USDT: per 1000 BONK). MEXC gives no mark price here.
    """
    positions = []
    for item in unwrap(raw) or []:
        instrument = instruments.get(item.get("symbol", ""))
        volume = _decimal(item.get("holdVol")) or Decimal(0)
        side = POSITION_SIDES.get(int(item.get("positionType") or 0))
        if instrument is None or volume <= 0 or side is None:
            continue
        unit_price = instrument.price_unit_tokens
        entry = _decimal(item.get("holdAvgPrice") or item.get("openAvgPrice")) or Decimal(0)
        liquidation = _positive(item.get("liquidatePrice"))
        positions.append(
            Position(
                exchange="mexc",
                symbol_raw=item["symbol"],
                token=instrument.token,
                side=side,
                qty_tokens=volume * instrument.qty_unit_tokens,
                entry_price=entry / unit_price,
                mark_price=None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(item["leverage"]) if item.get("leverage") else None,
                margin_mode=MARGIN_MODES.get(int(item.get("openType") or 0)),
                updated_ms=int(item.get("updateTime") or 0),
            )
        )
    return positions


def parse_balance(raw: dict[str, Any]) -> Balance:
    """GET /api/v1/private/account/assets — USDT equity, available balance and margin held by positions and orders."""
    for item in unwrap(raw) or []:
        if item.get("currency") == "USDT":
            margin = (_decimal(item.get("positionMargin")) or Decimal(0)) + (_decimal(item.get("frozenBalance")) or Decimal(0))
            return Balance(
                exchange="mexc",
                equity_usd=_decimal(item.get("equity")) or Decimal(0),
                available_usd=_decimal(item.get("availableBalance")) or Decimal(0),
                margin_used_usd=margin,
            )
    return Balance("mexc", Decimal(0), Decimal(0), Decimal(0))


def parse_funding(raw: dict[str, Any], since_ms: int) -> tuple[Decimal, bool]:
    """
    GET /api/v1/private/position/funding_records — (sum of funding since the moment, whether older pages may hold more).
    Records come newest first; positive funding was received.
    """
    data = unwrap(raw) or {}
    records = data.get("resultList") or []
    total = sum(
        (_decimal(item.get("funding")) or Decimal(0) for item in records if int(item.get("settleTime") or 0) >= since_ms),
        Decimal(0),
    )
    oldest = min((int(item.get("settleTime") or 0) for item in records), default=0)
    more = bool(records) and oldest >= since_ms and int(data.get("currentPage") or 1) < int(data.get("totalPage") or 1)
    return total, more


ORDER_SIDES = {(LegSide.LONG, True): 1, (LegSide.SHORT, False): 2, (LegSide.SHORT, True): 3, (LegSide.LONG, False): 4}
MARKET_ORDER_TYPE = 5
ORDER_FINAL_FILLED = 3
ORDER_FINAL_CLOSED = {4, 5}  # cancelled, invalid
STATUS_POLLS = 4
STATUS_POLL_INTERVAL_S = 0.15


def vol_value(units: Decimal) -> int | float:
    return int(units) if units == units.to_integral_value() else float(units)


def parse_order(
    data: dict[str, Any],
    instrument: Instrument,
    client_order_id: str,
    requested_tokens: Decimal,
    sent_ms: int,
    ack_ms: int | None,
) -> OrderReport:
    """GET /api/v1/private/order/external/{symbol}/{external_oid}: state 1 uninformed, 2 working, 3 done, 4 cancelled, 5 invalid."""
    state = int(data.get("state") or 0)
    filled = (_decimal(data.get("dealVol")) or Decimal(0)) * instrument.qty_unit_tokens
    average = _positive(data.get("dealAvgPrice"))
    if state == ORDER_FINAL_FILLED:
        outcome = OrderOutcome.FILLED if filled >= requested_tokens else OrderOutcome.PARTIAL
    elif state in ORDER_FINAL_CLOSED:
        outcome = OrderOutcome.PARTIAL if filled > 0 else OrderOutcome.REJECTED
    else:
        outcome = OrderOutcome.UNKNOWN
    fee = (_decimal(data.get("takerFee")) or Decimal(0)) + (_decimal(data.get("makerFee")) or Decimal(0))
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(data["orderId"]) if data.get("orderId") is not None else None,
        outcome=outcome,
        status=f"state_{state}",
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=average / instrument.price_unit_tokens if average else None,
        fee_usd=fee if str(data.get("feeCurrency") or "USDT") == "USDT" else None,
        error_code=str(data["errorCode"]) if data.get("errorCode") not in (None, 0) else None,
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=data,
    )


def parse_fills(raw: dict[str, Any], instrument: Instrument) -> list[FillReport]:
    """GET /api/v1/private/order/deal_details/{order_id} — vol in contracts, price per base unit."""
    return [
        FillReport(
            exchange_fill_id=str(item["id"]),
            ts_ms=int(item.get("timestamp") or 0),
            price=(_decimal(item.get("price")) or Decimal(0)) / instrument.price_unit_tokens,
            qty_tokens=(_decimal(item.get("vol")) or Decimal(0)) * instrument.qty_unit_tokens,
            fee=abs(_decimal(item.get("fee")) or Decimal(0)),
            fee_asset=str(item.get("feeCurrency") or "USDT"),
            is_maker=None if item.get("isTaker") is None else not bool(item.get("isTaker")),
        )
        for item in unwrap(raw) or []
    ]


def submitted_order_id(raw: dict[str, Any]) -> str | None:
    """POST /api/v1/private/order/submit answers data as the order id, or as an object carrying orderId."""
    data = unwrap(raw)
    if isinstance(data, dict):
        return str(data.get("orderId")) if data.get("orderId") is not None else None
    return None if data is None else str(data)


def parse_one_way(raw: dict[str, Any]) -> bool | None:
    """GET /api/v1/private/position/position_mode."""
    data = unwrap(raw)
    return POSITION_MODES.get(int(data)) if data is not None else None


def parse_usdt_assets(raw: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """GET /api/v1/private/account/assets — equity and available balance of USDT; zero when absent."""
    for item in unwrap(raw) or []:
        if item.get("currency") == "USDT":
            wallet = _decimal(item.get("equity", item.get("cashBalance"))) or Decimal(0)
            return wallet, _decimal(item.get("availableBalance")) or Decimal(0)
    return Decimal(0), Decimal(0)


def parse_fee_rates(raw: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    """
    Account fee rate or contract detail. Both carry fractions; field names differ between endpoints,
    so the known spellings are tried in order. Returns percent (taker, maker) or None.
    """
    data = unwrap(raw)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None
    taker = next((data[key] for key in ("takerFeeRate", "takerFee", "taker") if data.get(key) is not None), None)
    maker = next((data[key] for key in ("makerFeeRate", "makerFee", "maker") if data.get(key) is not None), None)
    if taker is None or maker is None:
        return None
    return Decimal(str(taker)) * 100, Decimal(str(maker)) * 100


class MexcAdapter:
    name = "mexc"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, timeout_s: float = 10) -> None:
        self._client = with_shared_context(ccxt.mexc(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
                "options": {"defaultType": "swap"},
            }
        ))
        # Probes get their own client so the rate limiter never queues them behind catalog requests.
        self._probe_client = with_shared_context(ccxt.mexc({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self._client.contract_public_get_detail())

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(await self._client.contract_public_get_ticker())

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        return parse_positions(await self._client.contract_private_get_position_open_positions(), instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(await self._client.contract_private_get_account_assets())

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        total = Decimal(0)
        for page in range(1, 21):
            raw = await self._client.contract_private_get_position_funding_records(
                {"symbol": symbol_raw, "page_num": page, "page_size": 100}
            )
            amount, more = parse_funding(raw, since_ms)
            total += amount
            if not more:
                break
        return total

    def login_message(self, now_ms: int) -> str:
        """Private websocket login; the signature is built here so the secret never leaves the adapter."""
        from app.market.private_streams import mexc_login_message

        return mexc_login_message(self._client.apiKey, self._client.secret, now_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        # Without an open position MEXC keeps leverage per side and margin mode: set both sides.
        for position_type in (1, 2):
            raw = await self._client.contract_private_post_position_change_leverage(
                {"symbol": instrument.symbol_raw, "leverage": leverage, "openType": 1 if isolated else 2, "positionType": position_type}
            )
            unwrap(raw)

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
            "vol": vol_value(qty_units),
            "side": ORDER_SIDES[(leg, opening)],
            "type": MARKET_ORDER_TYPE,
            "openType": 1 if isolated else 2,
            "leverage": leverage,
            "externalOid": client_order_id,
        }
        if not opening:
            params["reduceOnly"] = True
        sent = int(now_ms())
        try:
            order_id = submitted_order_id(await self._client.contract_private_post_order_submit(params))
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
            raw = await self._client.contract_private_get_order_external_symbol_external_oid(
                {"symbol": instrument.symbol_raw, "external_oid": client_order_id}
            )
            data = unwrap(raw)
        except Exception as exc:
            text = str(exc).lower()
            missing = "not exist" in text or "not found" in text
            return OrderReport(
                client_order_id, None, OrderOutcome.REJECTED if missing else OrderOutcome.UNKNOWN, "not_found" if missing else "error",
                requested_tokens, Decimal(0), None, None, error_code(exc), describe_error("order_status", exc), sent, None,
            )
        if not data:
            return OrderReport(client_order_id, None, OrderOutcome.UNKNOWN, "empty", requested_tokens, Decimal(0), None, None, None, None, sent, None)
        return parse_order(data, instrument, client_order_id, requested_tokens, sent, int(now_ms()))

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        raw = await self._client.contract_private_get_order_deal_details_order_id({"order_id": report.exchange_order_id})
        return parse_fills(raw, instrument)

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.contract_public_get_ping()
        received = now_ms()
        server = int(unwrap(response))
        return ClockProbe(
            ping_ms=round(received - sent),
            clock_offset_ms=clock_offset_ms(sent, received, server),
            server_ts_ms=server,
        )

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        raw_mode = await step(errors, "position_mode", self._client.contract_private_get_position_position_mode())
        raw_assets = await step(errors, "balance", self._client.contract_private_get_account_assets())

        raw_fees = await step(notes, "fees", self._client.contract_private_get_account_contract_fee_rate())
        fees = parse(notes, "fees", parse_fee_rates, raw_fees)
        if fees is None:
            # Public contract detail carries the default rates when the account endpoint is unavailable.
            raw_detail = await step(
                notes, "fees_contract_detail", self._client.contract_public_get_detail({"symbol": FEE_REFERENCE_SYMBOL})
            )
            fees = parse(notes, "fees_contract_detail", parse_fee_rates, raw_detail)

        wallet, available = parse(errors, "balance", parse_usdt_assets, raw_assets) or (None, None)
        taker, maker = fees or (None, None)
        return AccountFacts(
            permissions=KeyPermissions(),
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
