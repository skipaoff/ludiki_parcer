"""
VFP: Bybit USDT linear perpetuals behind the adapter contract (API v5) — contracts, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: Bybit changes its v5 API or the terminal needs more from Bybit.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Changing account-wide settings — on a unified trading account the margin mode belongs to the whole account, so the
   adapter sets only the leverage of a symbol and reports the margin mode it finds.
3. Trusting the submit answer as a result — it carries only the order id, so the order status is read right after.

Endpoints and shapes: docs/EXCHANGES.md, section Bybit. Public shapes checked live on 15.09.2026, private ones follow the documentation.
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

EXCHANGE = "bybit"
LINEAR = {"category": "linear"}
FEE_REFERENCE_SYMBOL = "BTCUSDT"
LEVERAGE_NOT_MODIFIED = "110043"
STATUS_POLLS = 4
STATUS_POLL_INTERVAL_S = 0.15
FUNDING_WINDOW_MS = 7 * 24 * 3600 * 1000  # the transaction log answers at most 7 days per request
FINAL_WITHOUT_FULL_FILL = {"Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def result_of(raw: Any) -> Any:
    """v5 answers {"retCode": 0, "retMsg": "OK", "result": {...}, "time": ...}; ccxt raises on other codes."""
    return raw.get("result", raw) if isinstance(raw, dict) else raw


def units_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_instruments(items: list[dict[str, Any]]) -> list[Instrument]:
    """
    GET /v5/market/instruments-info?category=linear — USDT-settled perpetuals that are trading.
    lotSizeFilter.qtyStep and minOrderQty are in coins of the symbol (1000PEPEUSDT: lots of 1000 PEPE), maxMktOrderQty
    limits a market order, minNotionalValue is the minimum order value in USDT.
    """
    instruments = []
    for item in items:
        symbol = str(item.get("symbol") or "")
        if (
            item.get("contractType") != "LinearPerpetual"
            or item.get("status") != "Trading"
            or item.get("quoteCoin") != "USDT"
            or item.get("settleCoin") != "USDT"
            or to_bool(item.get("isPreListing")) is True
        ):
            continue
        parsed = parse_symbol(symbol, EXCHANGE)
        lot = item.get("lotSizeFilter") or {}
        step_units = _positive(lot.get("qtyStep"))
        if parsed is None or step_units is None:
            continue
        instruments.append(
            Instrument(
                exchange=EXCHANGE,
                symbol_raw=symbol,
                token=parsed.token,
                qty_unit_tokens=parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=step_units,
                min_qty_units=_decimal(lot.get("minOrderQty")) or step_units,
                max_market_qty_units=_positive(lot.get("maxMktOrderQty")),
                min_notional_usd=_decimal(lot.get("minNotionalValue")) or Decimal(0),
                price_tick=_positive((item.get("priceFilter") or {}).get("tickSize")),
            )
        )
    return instruments


def parse_quotes(tickers: list[dict[str, Any]]) -> dict[str, Quote]:
    """GET /v5/market/tickers?category=linear — bid1Price, ask1Price, markPrice, indexPrice, turnover24h (USDT)."""
    return {
        item["symbol"]: Quote(
            bid=_positive(item.get("bid1Price")),
            ask=_positive(item.get("ask1Price")),
            mark=_positive(item.get("markPrice")),
            index=_positive(item.get("indexPrice")),
            volume24h_usd=_positive(item.get("turnover24h")),
        )
        for item in tickers
        if item.get("symbol")
    }


def parse_positions(raw: list[dict[str, Any]], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    GET /v5/position/list?category=linear&settleCoin=USDT — size in coins without sign, side Buy or Sell, avgPrice,
    markPrice, liqPrice (empty when none), leverage, tradeMode 0 cross / 1 isolated (absent on unified accounts).
    """
    positions = []
    for item in raw:
        instrument = instruments.get(item.get("symbol", ""))
        size = _decimal(item.get("size")) or Decimal(0)
        if instrument is None or size == 0 or item.get("side") not in ("Buy", "Sell"):
            continue
        unit_price = instrument.price_unit_tokens
        mark = _positive(item.get("markPrice"))
        liquidation = _positive(item.get("liqPrice"))
        trade_mode = item.get("tradeMode")
        positions.append(
            Position(
                exchange=EXCHANGE,
                symbol_raw=item["symbol"],
                token=instrument.token,
                side=LegSide.LONG if item["side"] == "Buy" else LegSide.SHORT,
                qty_tokens=abs(size) * instrument.qty_unit_tokens,
                entry_price=(_decimal(item.get("avgPrice")) or Decimal(0)) / unit_price,
                mark_price=mark / unit_price if mark else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(Decimal(str(item["leverage"]))) if item.get("leverage") else None,
                margin_mode=None if trade_mode in (None, "") else ("isolated" if int(trade_mode) == 1 else "cross"),
                updated_ms=int(item.get("updatedTime") or 0),
            )
        )
    return positions


def _account(result: Any) -> dict[str, Any]:
    items = result.get("list") if isinstance(result, dict) else None
    return items[0] if items else {}


def parse_balance(result: Any) -> Balance:
    """GET /v5/account/wallet-balance?accountType=UNIFIED — totalEquity, totalAvailableBalance, totalInitialMargin (USD)."""
    account = _account(result)
    return Balance(
        exchange=EXCHANGE,
        equity_usd=_decimal(account.get("totalEquity")) or Decimal(0),
        available_usd=_decimal(account.get("totalAvailableBalance")) or Decimal(0),
        margin_used_usd=_decimal(account.get("totalInitialMargin")) or Decimal(0),
    )


def parse_usdt_balance(result: Any) -> tuple[Decimal, Decimal]:
    """USDT wallet balance of the unified account and what it can still open with."""
    account = _account(result)
    usdt = next((coin for coin in account.get("coin") or [] if coin.get("coin") == "USDT"), {})
    return _decimal(usdt.get("walletBalance")) or Decimal(0), _decimal(account.get("totalAvailableBalance")) or Decimal(0)


def parse_funding(entries: list[dict[str, Any]], symbol: str, since_ms: int) -> Decimal:
    """GET /v5/account/transaction-log?type=SETTLEMENT — funding: positive received, negative paid."""
    return sum(
        (
            _decimal(item.get("funding")) or Decimal(0)
            for item in entries
            if item.get("type") == "SETTLEMENT" and item.get("symbol") == symbol and int(item.get("transactionTime") or 0) >= since_ms
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
    """GET /v5/order/realtime or /v5/order/history — orderStatus, cumExecQty in coins, avgPrice, rejectReason."""
    status = str(raw.get("orderStatus") or "")
    filled = (_decimal(raw.get("cumExecQty")) or Decimal(0)) * instrument.qty_unit_tokens
    average = _positive(raw.get("avgPrice"))
    if status == "Filled":
        outcome = OrderOutcome.FILLED if filled >= requested_tokens else OrderOutcome.PARTIAL
    elif status in FINAL_WITHOUT_FULL_FILL:
        outcome = OrderOutcome.PARTIAL if filled > 0 else OrderOutcome.REJECTED
    else:
        outcome = OrderOutcome.UNKNOWN
    reason = raw.get("rejectReason")
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(raw["orderId"]) if raw.get("orderId") else None,
        outcome=outcome,
        status=status,
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=average / instrument.price_unit_tokens if average else None,
        fee_usd=None,
        error_code=None if outcome is not OrderOutcome.REJECTED else str(reason or status),
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=raw,
    )


def parse_fills(raw: list[dict[str, Any]], instrument: Instrument) -> list[FillReport]:
    """GET /v5/execution/list?orderId= — execId, execPrice, execQty (coins), execFee (USDT), execTime, isMaker."""
    return [
        FillReport(
            exchange_fill_id=str(item["execId"]),
            ts_ms=int(item.get("execTime") or 0),
            price=(_decimal(item.get("execPrice")) or Decimal(0)) / instrument.price_unit_tokens,
            qty_tokens=(_decimal(item.get("execQty")) or Decimal(0)) * instrument.qty_unit_tokens,
            fee=abs(_decimal(item.get("execFee")) or Decimal(0)),
            fee_asset=str(item.get("feeCurrency") or "USDT"),
            is_maker=to_bool(item.get("isMaker")),
        )
        for item in raw
        if item.get("execId")
    ]


def parse_permissions(result: Any) -> KeyPermissions:
    """
    GET /v5/user/query-api — readOnly 1 means no trading; permissions.Wallet lists "Withdraw" when the key can withdraw;
    ContractTrade or Derivatives carry futures trading; ips ["*"] means no IP binding.
    """
    permissions = result.get("permissions") or {}
    trading = bool(permissions.get("ContractTrade") or permissions.get("Derivatives"))
    ips = result.get("ips")
    return KeyPermissions(
        reading=True,
        futures=trading and str(result.get("readOnly")) != "1",
        withdrawals="Withdraw" in (permissions.get("Wallet") or []),
        ip_restricted=None if not isinstance(ips, list) else bool(ips) and ips != ["*"],
    )


def parse_one_way(result: Any) -> bool | None:
    """Bybit has no position-mode query: positionIdx 0 of an existing position means one-way, 1 or 2 hedge; none — unknown."""
    items = result.get("list") if isinstance(result, dict) else None
    if not items:
        return None
    return all(int(item.get("positionIdx") or 0) == 0 for item in items)


def parse_fee_rates(result: Any) -> tuple[Decimal, Decimal]:
    """GET /v5/account/fee-rate?category=linear&symbol= — takerFeeRate, makerFeeRate as fractions; returned as percent."""
    rates = result["list"][0]
    return Decimal(str(rates["takerFeeRate"])) * 100, Decimal(str(rates["makerFeeRate"])) * 100


class BybitAdapter:
    name = EXCHANGE

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, timeout_s: float = 10) -> None:
        config = {"apiKey": api_key or "", "secret": api_secret or "", "enableRateLimit": True, "timeout": int(timeout_s * 1000),
                  "options": {"defaultType": "swap"}}
        self._client = with_shared_context(ccxt.bybit(config))
        # Probes get their own client so the rate limiter never queues them behind catalog requests.
        self._probe_client = with_shared_context(ccxt.bybit({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        items: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            params = {**LINEAR, "limit": 1000, **({"cursor": cursor} if cursor else {})}
            result = result_of(await self._client.public_get_v5_market_instruments_info(params))
            items += result.get("list") or []
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        return parse_instruments(items)

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(result_of(await self._client.public_get_v5_market_tickers(dict(LINEAR))).get("list") or [])

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.public_get_v5_market_time()
        received = now_ms()
        server = int(response.get("time") or int(result_of(response)["timeNano"]) // 1_000_000)
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server), server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        raw_permissions = await step(errors, "permissions", self._client.private_get_v5_user_query_api())
        raw_balance = await step(errors, "balance", self._client.private_get_v5_account_wallet_balance({"accountType": "UNIFIED"}))
        raw_positions = await step(notes, "position_mode", self._client.private_get_v5_position_list({**LINEAR, "settleCoin": "USDT"}))
        raw_fees = await step(notes, "fees", self._client.private_get_v5_account_fee_rate({**LINEAR, "symbol": FEE_REFERENCE_SYMBOL}))
        permissions = parse(errors, "permissions", lambda raw: parse_permissions(result_of(raw)), raw_permissions) or KeyPermissions()
        wallet, available = parse(errors, "balance", lambda raw: parse_usdt_balance(result_of(raw)), raw_balance) or (None, None)
        taker, maker = parse(notes, "fees", lambda raw: parse_fee_rates(result_of(raw)), raw_fees) or (None, None)
        return AccountFacts(
            permissions=permissions,
            one_way_position_mode=parse(notes, "position_mode", lambda raw: parse_one_way(result_of(raw)), raw_positions),
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
        result = result_of(await self._client.private_get_v5_position_list({**LINEAR, "settleCoin": "USDT", "limit": 200}))
        return parse_positions(result.get("list") or [], instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(result_of(await self._client.private_get_v5_account_wallet_balance({"accountType": "UNIFIED"})))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        entries: list[dict[str, Any]] = []
        start, now = since_ms, int(now_ms())
        while start < now:
            end = min(start + FUNDING_WINDOW_MS, now)
            cursor = None
            for _ in range(20):
                params = {"accountType": "UNIFIED", **LINEAR, "currency": "USDT", "type": "SETTLEMENT", "startTime": start, "endTime": end,
                          "limit": 50, **({"cursor": cursor} if cursor else {})}
                result = result_of(await self._client.private_get_v5_account_transaction_log(params))
                entries += result.get("list") or []
                cursor = result.get("nextPageCursor")
                if not cursor:
                    break
            start = end
        return parse_funding(entries, symbol_raw, since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        try:
            await self._client.private_post_v5_position_set_leverage(
                {**LINEAR, "symbol": instrument.symbol_raw, "buyLeverage": str(leverage), "sellLeverage": str(leverage)}
            )
        except Exception as exc:
            if LEVERAGE_NOT_MODIFIED not in str(exc):
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
            **LINEAR,
            "symbol": instrument.symbol_raw,
            "side": "Buy" if (leg is LegSide.LONG) == opening else "Sell",
            "orderType": "Market",
            "qty": units_text(qty_units),
            "orderLinkId": client_order_id,
            "positionIdx": 0,
        }
        if not opening:
            params["reduceOnly"] = True
        sent = int(now_ms())
        try:
            result = result_of(await self._client.private_post_v5_order_create(params))
        except Exception as exc:
            unknown = is_unknown_outcome_error(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN if unknown else OrderOutcome.REJECTED, "error", requested,
                Decimal(0), None, None, error_code(exc), describe_error("order", exc), sent, None if unknown else int(now_ms()),
            )
        ack = int(now_ms())
        order_id = str(result.get("orderId")) if isinstance(result, dict) and result.get("orderId") else None
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
        query = {**LINEAR, "symbol": instrument.symbol_raw, "orderLinkId": client_order_id}
        try:
            for call in (self._client.private_get_v5_order_realtime, self._client.private_get_v5_order_history):
                items = result_of(await call(dict(query))).get("list") or []
                if items:
                    return parse_order(items[0], instrument, client_order_id, requested_tokens, sent, int(now_ms()))
        except Exception as exc:
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN, "error", requested_tokens, Decimal(0), None, None,
                error_code(exc), describe_error("order_status", exc), sent, None,
            )
        return OrderReport(
            client_order_id, None, OrderOutcome.REJECTED, "not_found", requested_tokens, Decimal(0), None, None,
            "not_found", "order not found among open or past orders", sent, None,
        )

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        result = result_of(await self._client.private_get_v5_execution_list({**LINEAR, "symbol": instrument.symbol_raw, "orderId": report.exchange_order_id}))
        return parse_fills(result.get("list") or [], instrument)
