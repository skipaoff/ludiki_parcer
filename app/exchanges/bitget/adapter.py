"""
VFP: Bitget USDT-M perpetual futures behind the adapter contract (API v2) — contracts, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: Bitget changes its v2 mix API or the terminal needs more from Bitget.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Reading unknown permission codes as safe — Bitget documents its key authorities loosely, so withdrawal rights stay
   unknown unless a withdrawal authority is actually listed.
3. Trusting the submit answer as a result — it carries only ids, so the order status is read right after.

Keys: API key, secret and the passphrase chosen when the key was made.
Endpoints and shapes: docs/EXCHANGES.md, section Bitget. Public shapes checked live on 15.09.2026, private ones follow the documentation.
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
from app.exchanges.ccxt_support import describe_error, error_code, is_unknown_outcome_error, now_ms, parse, step
from app.system.tls import with_shared_context

EXCHANGE = "bitget"
PRODUCT = {"productType": "USDT-FUTURES"}
MARGIN = {"marginCoin": "USDT"}
FEE_REFERENCE_SYMBOL = "BTCUSDT"
STATUS_POLLS = 4
STATUS_POLL_INTERVAL_S = 0.15
FINAL_WITHOUT_FULL_FILL = {"canceled", "cancelled", "rejected"}
NOT_FOUND_MARKERS = ("40768", "does not exist", "not exist")


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def data_of(raw: Any) -> Any:
    """v2 answers {"code": "00000", "msg": "success", "requestTime": ..., "data": ...}; ccxt raises on other codes."""
    return raw.get("data", raw) if isinstance(raw, dict) else raw


def units_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_instruments(contracts: list[dict[str, Any]]) -> list[Instrument]:
    """
    GET /api/v2/mix/market/contracts?productType=USDT-FUTURES — perpetuals with symbolStatus normal.
    sizeMultiplier is the quantity step in coins of the symbol, minTradeNum the minimum, maxMarketOrderQty the market order
    maximum, minTradeUSDT the minimum order value; the price tick is priceEndStep × 10^-pricePlace.
    """
    instruments = []
    for item in contracts:
        symbol = str(item.get("symbol") or "")
        if (
            item.get("symbolType") != "perpetual"
            or item.get("symbolStatus") != "normal"
            or item.get("quoteCoin") != "USDT"
            or "USDT" not in (item.get("supportMarginCoins") or ["USDT"])
        ):
            continue
        parsed = parse_symbol(symbol, EXCHANGE)
        step_units = _positive(item.get("sizeMultiplier"))
        if parsed is None or step_units is None:
            continue
        place = item.get("pricePlace")
        tick = None
        if place not in (None, ""):
            tick = (_decimal(item.get("priceEndStep")) or Decimal(1)) * Decimal(1).scaleb(-int(place))
        instruments.append(
            Instrument(
                exchange=EXCHANGE,
                symbol_raw=symbol,
                token=parsed.token,
                qty_unit_tokens=parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=step_units,
                min_qty_units=_decimal(item.get("minTradeNum")) or step_units,
                max_market_qty_units=_positive(item.get("maxMarketOrderQty")),
                min_notional_usd=_decimal(item.get("minTradeUSDT")) or Decimal(0),
                price_tick=tick,
            )
        )
    return instruments


def parse_quotes(tickers: list[dict[str, Any]]) -> dict[str, Quote]:
    """GET /api/v2/mix/market/tickers?productType=USDT-FUTURES — bidPr, askPr, markPrice, indexPrice, usdtVolume."""
    return {
        item["symbol"]: Quote(
            bid=_positive(item.get("bidPr")),
            ask=_positive(item.get("askPr")),
            mark=_positive(item.get("markPrice")),
            index=_positive(item.get("indexPrice")),
            volume24h_usd=_positive(item.get("usdtVolume")) or _positive(item.get("quoteVolume")),
        )
        for item in tickers
        if item.get("symbol")
    }


def parse_positions(raw: list[dict[str, Any]], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    GET /api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT — holdSide long/short, total in coins,
    openPriceAvg, markPrice, liquidationPrice, leverage, marginMode isolated/crossed, uTime.
    """
    positions = []
    for item in raw:
        instrument = instruments.get(item.get("symbol", ""))
        total = _decimal(item.get("total")) or Decimal(0)
        if instrument is None or total == 0 or item.get("holdSide") not in ("long", "short"):
            continue
        unit_price = instrument.price_unit_tokens
        mark = _positive(item.get("markPrice"))
        liquidation = _positive(item.get("liquidationPrice"))
        mode = str(item.get("marginMode") or "")
        positions.append(
            Position(
                exchange=EXCHANGE,
                symbol_raw=item["symbol"],
                token=instrument.token,
                side=LegSide.LONG if item["holdSide"] == "long" else LegSide.SHORT,
                qty_tokens=abs(total) * instrument.qty_unit_tokens,
                entry_price=(_decimal(item.get("openPriceAvg")) or Decimal(0)) / unit_price,
                mark_price=mark / unit_price if mark else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(Decimal(str(item["leverage"]))) if item.get("leverage") else None,
                margin_mode="isolated" if mode == "isolated" else "cross" if mode == "crossed" else None,
                updated_ms=int(item.get("uTime") or item.get("cTime") or 0),
            )
        )
    return positions


def _usdt_account(raw: Any) -> dict[str, Any]:
    items = raw if isinstance(raw, list) else [raw]
    return next((item for item in items if isinstance(item, dict) and item.get("marginCoin", "USDT") == "USDT"), {})


def parse_balance(raw: Any) -> Balance:
    """GET /api/v2/mix/account/accounts?productType=USDT-FUTURES — accountEquity, available, crossedMargin + isolatedMargin."""
    account = _usdt_account(raw)
    equity = _decimal(account.get("accountEquity")) or _decimal(account.get("usdtEquity")) or Decimal(0)
    available = _decimal(account.get("available")) or Decimal(0)
    margins = [_decimal(account.get(name)) for name in ("crossedMargin", "isolatedMargin")]
    used = sum((value for value in margins if value is not None), Decimal(0)) if any(value is not None for value in margins) else equity - available
    return Balance(exchange=EXCHANGE, equity_usd=equity, available_usd=available, margin_used_usd=used)


def parse_usdt_balance(raw: Any) -> tuple[Decimal, Decimal]:
    account = _usdt_account(raw)
    return _decimal(account.get("accountEquity")) or Decimal(0), _decimal(account.get("available")) or Decimal(0)


def parse_funding(bills: list[dict[str, Any]], symbol: str, since_ms: int) -> Decimal:
    """GET /api/v2/mix/account/bill?businessType=contract_settle_fee — amount: positive received, negative paid."""
    return sum(
        (
            _decimal(item.get("amount")) or Decimal(0)
            for item in bills
            if item.get("businessType") == "contract_settle_fee" and item.get("symbol", symbol) == symbol and int(item.get("cTime") or 0) >= since_ms
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
    """GET /api/v2/mix/order/detail — state live/partially_filled/filled/canceled, baseVolume filled in coins, priceAvg."""
    state = str(raw.get("state") or raw.get("status") or "")
    filled = (_decimal(raw.get("baseVolume")) or Decimal(0)) * instrument.qty_unit_tokens
    average = _positive(raw.get("priceAvg"))
    if state == "filled":
        outcome = OrderOutcome.FILLED if filled >= requested_tokens else OrderOutcome.PARTIAL
    elif state in FINAL_WITHOUT_FULL_FILL:
        outcome = OrderOutcome.PARTIAL if filled > 0 else OrderOutcome.REJECTED
    else:
        outcome = OrderOutcome.UNKNOWN
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(raw["orderId"]) if raw.get("orderId") else None,
        outcome=outcome,
        status=state,
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=average / instrument.price_unit_tokens if average else None,
        fee_usd=None,
        error_code=None if outcome is not OrderOutcome.REJECTED else state,
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=raw,
    )


def parse_fills(raw: Any, instrument: Instrument) -> list[FillReport]:
    """GET /api/v2/mix/order/fills?orderId= — fillList: tradeId, price, baseVolume (coins), feeDetail totalFee, cTime, tradeScope."""
    items = raw.get("fillList") if isinstance(raw, dict) else raw
    fills = []
    for item in items or []:
        fee_detail = (item.get("feeDetail") or [{}])[0]
        fills.append(
            FillReport(
                exchange_fill_id=str(item["tradeId"]),
                ts_ms=int(item.get("cTime") or 0),
                price=(_decimal(item.get("price")) or Decimal(0)) / instrument.price_unit_tokens,
                qty_tokens=(_decimal(item.get("baseVolume")) or Decimal(0)) * instrument.qty_unit_tokens,
                fee=abs(_decimal(fee_detail.get("totalFee")) or Decimal(0)),
                fee_asset=str(fee_detail.get("feeCoin") or "USDT"),
                is_maker=None if item.get("tradeScope") is None else item.get("tradeScope") == "maker",
            )
        )
    return fills


def parse_permissions(raw: Any) -> KeyPermissions:
    """
    GET /api/v2/spot/account/info — authorities (codes such as "trade", "readonly", "coow", "wtw"…) and ips.
    Withdrawal is True only when a withdrawal authority is listed, otherwise unknown: the codes are loosely documented.
    """
    authorities = [str(code).lower() for code in raw.get("authorities") or []]
    withdraw = any("withdraw" in code or code.startswith("wt") for code in authorities)
    futures = any(code in ("trade", "contract") or code.startswith(("co", "cp")) for code in authorities)
    ips = raw.get("ips")
    return KeyPermissions(
        reading=bool(authorities) or None,
        futures=True if futures else None,
        withdrawals=True if withdraw else None,
        ip_restricted=None if ips is None else bool(str(ips).strip()),
    )


def parse_one_way(raw: Any) -> bool | None:
    """GET /api/v2/mix/account/account — posMode one_way_mode or hedge_mode."""
    mode = raw.get("posMode") if isinstance(raw, dict) else None
    return None if mode is None else mode == "one_way_mode"


def parse_fee_rates(raw: Any) -> tuple[Decimal, Decimal]:
    """GET /api/v2/common/trade-rate — takerFeeRate, makerFeeRate as fractions; returned as percent."""
    return Decimal(str(raw["takerFeeRate"])) * 100, Decimal(str(raw["makerFeeRate"])) * 100


def _not_found(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, ccxt.OrderNotFound) or any(marker in text for marker in NOT_FOUND_MARKERS)


class BitgetAdapter:
    name = EXCHANGE

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, passphrase: str | None = None, timeout_s: float = 10) -> None:
        config = {"apiKey": api_key or "", "secret": api_secret or "", "password": passphrase or "", "enableRateLimit": True,
                  "timeout": int(timeout_s * 1000), "options": {"defaultType": "swap"}}
        self._client = with_shared_context(ccxt.bitget(config))
        self._probe_client = with_shared_context(ccxt.bitget({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(data_of(await self._client.public_mix_get_v2_mix_market_contracts(dict(PRODUCT))) or [])

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(data_of(await self._client.public_mix_get_v2_mix_market_tickers(dict(PRODUCT))) or [])

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.public_common_get_v2_public_time()
        received = now_ms()
        server = int(data_of(response)["serverTime"])
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server), server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        raw_permissions = await step(errors, "permissions", self._client.private_spot_get_v2_spot_account_info())
        raw_account = await step(errors, "account", self._client.private_mix_get_v2_mix_account_account({**PRODUCT, **MARGIN, "symbol": FEE_REFERENCE_SYMBOL}))
        raw_fees = await step(notes, "fees", self._client.private_common_get_v2_common_trade_rate({"symbol": FEE_REFERENCE_SYMBOL, "businessType": "mix"}))
        permissions = parse(errors, "permissions", lambda raw: parse_permissions(data_of(raw)), raw_permissions) or KeyPermissions()
        wallet, available = parse(errors, "account", lambda raw: parse_usdt_balance(data_of(raw)), raw_account) or (None, None)
        taker, maker = parse(notes, "fees", lambda raw: parse_fee_rates(data_of(raw)), raw_fees) or (None, None)
        return AccountFacts(
            permissions=permissions,
            one_way_position_mode=parse(errors, "account", lambda raw: parse_one_way(data_of(raw)), raw_account),
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
        return parse_positions(data_of(await self._client.private_mix_get_v2_mix_position_all_position({**PRODUCT, **MARGIN})) or [], instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(data_of(await self._client.private_mix_get_v2_mix_account_accounts(dict(PRODUCT))))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raw = data_of(
            await self._client.private_mix_get_v2_mix_account_bill(
                {**PRODUCT, "symbol": symbol_raw, "businessType": "contract_settle_fee", "startTime": since_ms, "limit": 100}
            )
        )
        bills = raw.get("bills") if isinstance(raw, dict) else raw
        return parse_funding(bills or [], symbol_raw, since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        common = {**PRODUCT, **MARGIN, "symbol": instrument.symbol_raw}
        try:
            await self._client.private_mix_post_v2_mix_account_set_margin_mode({**common, "marginMode": "isolated" if isolated else "crossed"})
        except Exception as exc:
            # Unchanged mode answers with an error on some accounts; the leverage call below still has to succeed.
            if "same" not in str(exc).lower() and "40756" not in str(exc):
                raise
        await self._client.private_mix_post_v2_mix_account_set_leverage({**common, "leverage": str(leverage)})

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
            **PRODUCT,
            **MARGIN,
            "symbol": instrument.symbol_raw,
            "marginMode": "isolated" if isolated else "crossed",
            "size": units_text(qty_units),
            "side": "buy" if (leg is LegSide.LONG) == opening else "sell",
            "orderType": "market",
            "clientOid": client_order_id,
            "reduceOnly": "NO" if opening else "YES",
        }
        sent = int(now_ms())
        try:
            data = data_of(await self._client.private_mix_post_v2_mix_order_place_order(params))
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
            data = data_of(await self._client.private_mix_get_v2_mix_order_detail({**PRODUCT, "symbol": instrument.symbol_raw, "clientOid": client_order_id}))
        except Exception as exc:
            missing = _not_found(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.REJECTED if missing else OrderOutcome.UNKNOWN, "not_found" if missing else "error",
                requested_tokens, Decimal(0), None, None, error_code(exc), describe_error("order_status", exc), sent, None,
            )
        if not isinstance(data, dict) or not data:
            return OrderReport(client_order_id, None, OrderOutcome.UNKNOWN, "empty", requested_tokens, Decimal(0), None, None, None, None, sent, None)
        return parse_order(data, instrument, client_order_id, requested_tokens, sent, int(now_ms()))

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        raw = data_of(await self._client.private_mix_get_v2_mix_order_fills({**PRODUCT, "symbol": instrument.symbol_raw, "orderId": report.exchange_order_id}))
        return parse_fills(raw, instrument)
