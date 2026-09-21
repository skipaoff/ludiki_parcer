"""
VFP: Gate USDT perpetual futures behind the adapter contract — contracts, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: Gate changes its futures API v4 or the terminal needs more from Gate.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Reading "order not found" as "never placed" — Gate finds an order by its custom text only for 60 seconds after it
   finished, so a miss is double-checked against the contract's recent orders before it counts as a rejection.
3. Fractional contract sizes — contracts that allow them are still traded in whole contracts, always a valid size.

Endpoints and shapes: docs/EXCHANGES.md, section Gate. Public shapes checked live on 14.09.2026, private ones follow the documentation.
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
from app.exchanges.ccxt_support import describe_error, error_code, is_unknown_outcome_error, now_ms, parse, step
from app.system.tls import with_shared_context

SETTLE = {"settle": "usdt"}
# The contract list is 1.3 MB without compression, and from some networks Gate delivers ~60 KB/s per connection (18-21 s,
# past any sane timeout): it is fetched in pages of 100, ten at a time, which arrives in about four seconds.
CONTRACTS_PAGE = 100
CONTRACTS_PAGES_AT_ONCE = 10
CONTRACTS_MAX_WAVES = 10
CATALOG_TIMEOUT_S = 60  # the ticker list is 0.5 MB in one piece
FEE_REFERENCE_CONTRACT = "BTC_USDT"
RECENT_ORDERS_LIMIT = 100


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def custom_text(client_order_id: str) -> str:
    """Gate custom order ids must start with "t-"."""
    return client_order_id if client_order_id.startswith("t-") else f"t-{client_order_id}"


async def fetch_contract_pages(get_page: Any) -> list[dict[str, Any]]:
    """Every contract, pages fetched in parallel waves until a short page; a contract listed mid-way is counted once."""
    by_name: dict[str, dict[str, Any]] = {}
    for wave in range(CONTRACTS_MAX_WAVES):
        offsets = [(wave * CONTRACTS_PAGES_AT_ONCE + index) * CONTRACTS_PAGE for index in range(CONTRACTS_PAGES_AT_ONCE)]
        pages = await asyncio.gather(*(get_page({**SETTLE, "limit": CONTRACTS_PAGE, "offset": offset}) for offset in offsets))
        for page in pages:
            for item in page or []:
                by_name[str(item.get("name"))] = item
        if any(len(page or []) < CONTRACTS_PAGE for page in pages):
            break
    return list(by_name.values())


def parse_instruments(contracts: list[dict[str, Any]]) -> list[Instrument]:
    """
    GET /futures/usdt/contracts — trading perpetuals only. quanto_multiplier is tokens per contract (BTC_USDT: 0.0001 BTC,
    PEPE_USDT: 10,000,000 PEPE); size limits are in contracts; prices are per token.
    """
    instruments = []
    for item in contracts:
        name = str(item.get("name") or "")
        if (
            item.get("status", "trading") != "trading"
            or item.get("in_delisting")
            or item.get("is_pre_market")
            or item.get("type", "direct") != "direct"
            or not name.endswith("_USDT")
        ):
            continue
        parsed = parse_symbol(name, "gate")
        multiplier_tokens = _positive(item.get("quanto_multiplier"))
        if parsed is None or multiplier_tokens is None:
            continue
        minimum = _decimal(item.get("order_size_min")) or Decimal(0)
        market_max = _positive(item.get("market_order_size_max")) or _positive(item.get("order_size_max"))
        instruments.append(
            Instrument(
                exchange="gate",
                symbol_raw=name,
                token=parsed.token,
                qty_unit_tokens=multiplier_tokens * parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=Decimal(1),
                min_qty_units=max(minimum, Decimal(1)),
                max_market_qty_units=market_max,
                min_notional_usd=Decimal(0),
                price_tick=_positive(item.get("order_price_round")),
            )
        )
    return instruments


def parse_quotes(tickers: list[dict[str, Any]]) -> dict[str, Quote]:
    """GET /futures/usdt/tickers — highest_bid, lowest_ask, mark_price, index_price, volume_24h_quote (USDT turnover)."""
    return {
        item["contract"]: Quote(
            bid=_positive(item.get("highest_bid")),
            ask=_positive(item.get("lowest_ask")),
            mark=_positive(item.get("mark_price")),
            index=_positive(item.get("index_price")),
            volume24h_usd=_positive(item.get("volume_24h_quote")) or _positive(item.get("volume_24h_settle")),
        )
        for item in tickers
        if item.get("contract")
    }


def parse_positions(raw: list[dict[str, Any]], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    GET /futures/usdt/positions — size in contracts, positive long and negative short (one-way "single" mode);
    leverage 0 means cross margin. Prices per token.
    """
    positions = []
    for item in raw:
        instrument = instruments.get(item.get("contract", ""))
        size = _decimal(item.get("size")) or Decimal(0)
        if instrument is None or size == 0:
            continue
        unit_price = instrument.price_unit_tokens
        leverage = _decimal(item.get("leverage")) or Decimal(0)
        liquidation = _positive(item.get("liq_price"))
        mark = _positive(item.get("mark_price"))
        positions.append(
            Position(
                exchange="gate",
                symbol_raw=item["contract"],
                token=instrument.token,
                side=LegSide.LONG if size > 0 else LegSide.SHORT,
                qty_tokens=abs(size) * instrument.qty_unit_tokens,
                entry_price=(_decimal(item.get("entry_price")) or Decimal(0)) / unit_price,
                mark_price=mark / unit_price if mark else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(leverage) if leverage > 0 else _int_or_none(item.get("cross_leverage_limit")),
                margin_mode="isolated" if leverage > 0 else "cross",
                updated_ms=int(float(item.get("update_time") or 0) * 1000),
            )
        )
    return positions


def _int_or_none(value: Any) -> int | None:
    number = _decimal(value)
    return int(number) if number else None


def parse_balance(account: dict[str, Any]) -> Balance:
    """GET /futures/usdt/accounts — total wallet plus unrealised PnL is equity; margins of positions and orders are in use."""
    total = _decimal(account.get("total")) or Decimal(0)
    unrealised = _decimal(account.get("unrealised_pnl")) or Decimal(0)
    margin = (_decimal(account.get("position_margin")) or Decimal(0)) + (_decimal(account.get("order_margin")) or Decimal(0))
    return Balance(
        exchange="gate",
        equity_usd=total + unrealised,
        available_usd=_decimal(account.get("available")) or Decimal(0),
        margin_used_usd=margin,
    )


def parse_funding(entries: list[dict[str, Any]], contract: str, since_ms: int) -> Decimal:
    """GET /futures/usdt/account_book?type=fund — change is positive when funding was received."""
    total = Decimal(0)
    for item in entries:
        if item.get("type") != "fund":
            continue
        entry_contract = item.get("contract") or item.get("text") or ""
        if contract not in str(entry_contract):
            continue
        if float(item.get("time") or 0) * 1000 < since_ms:
            continue
        total += _decimal(item.get("change")) or Decimal(0)
    return total


def parse_order(
    raw: dict[str, Any],
    instrument: Instrument,
    client_order_id: str,
    requested_tokens: Decimal,
    sent_ms: int,
    ack_ms: int | None,
) -> OrderReport:
    """
    POST or GET /futures/usdt/orders — size and left are signed contracts; status finished or open;
    finish_as tells why it finished (filled, ioc, cancelled, reduce_only, ...). fill_price is the average price.
    """
    size = abs(_decimal(raw.get("size")) or Decimal(0))
    left = abs(_decimal(raw.get("left")) or Decimal(0))
    filled = (size - left) * instrument.qty_unit_tokens
    finished = raw.get("status") == "finished"
    if not finished:
        outcome = OrderOutcome.UNKNOWN
    elif filled >= requested_tokens:
        outcome = OrderOutcome.FILLED
    elif filled > 0:
        outcome = OrderOutcome.PARTIAL
    else:
        outcome = OrderOutcome.REJECTED
    average = _positive(raw.get("fill_price"))
    return OrderReport(
        client_order_id=client_order_id,
        exchange_order_id=str(raw["id"]) if raw.get("id") is not None else None,
        outcome=outcome,
        status=f"{raw.get('status')}:{raw.get('finish_as') or ''}",
        requested_tokens=requested_tokens,
        filled_tokens=filled,
        avg_price=average / instrument.price_unit_tokens if average else None,
        fee_usd=None,
        error_code=None if outcome is not OrderOutcome.REJECTED else str(raw.get("finish_as") or "not_filled"),
        error_message=None,
        sent_ts_ms=sent_ms,
        ack_ts_ms=ack_ms,
        response=raw,
    )


def parse_fills(raw: list[dict[str, Any]], instrument: Instrument) -> list[FillReport]:
    """GET /futures/usdt/my_trades?order= — size in signed contracts, fee in USDT (point_fee is paid in points)."""
    return [
        FillReport(
            exchange_fill_id=str(item["id"]),
            ts_ms=int(float(item.get("create_time") or 0) * 1000),
            price=(_decimal(item.get("price")) or Decimal(0)) / instrument.price_unit_tokens,
            qty_tokens=abs(_decimal(item.get("size")) or Decimal(0)) * instrument.qty_unit_tokens,
            fee=abs(_decimal(item.get("fee")) or Decimal(0)),
            fee_asset="USDT",
            is_maker=None if item.get("role") is None else item.get("role") == "maker",
        )
        for item in raw
    ]


def parse_fee_rates(raw: dict[str, Any], contract: str = FEE_REFERENCE_CONTRACT) -> tuple[Decimal, Decimal] | None:
    """GET /futures/usdt/fee — {"BTC_USDT": {"taker_fee": "0.00075", "maker_fee": "-0.0001"}} as fractions; returns percent."""
    rates = raw.get(contract) if isinstance(raw, dict) else None
    if not isinstance(rates, dict) or rates.get("taker_fee") is None:
        return None
    return Decimal(str(rates["taker_fee"])) * 100, Decimal(str(rates.get("maker_fee") or "0")) * 100


class GateAdapter:
    name = "gate"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, timeout_s: float = 10) -> None:
        self._client = with_shared_context(ccxt.gate(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
                "options": {"defaultType": "swap"},
            }
        ))
        # Probes get their own client so the rate limiter never queues them behind catalog requests.
        self._probe_client = with_shared_context(ccxt.gate({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))
        # Public catalog downloads are slow and large; their own client keeps the long timeout away from orders.
        self._catalog_client = with_shared_context(ccxt.gate({"enableRateLimit": False, "timeout": CATALOG_TIMEOUT_S * 1000}))

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()
        await self._catalog_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await fetch_contract_pages(self._catalog_client.public_futures_get_settle_contracts))

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(await self._catalog_client.public_futures_get_settle_tickers(dict(SETTLE)))

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.public_spot_get_time()
        received = now_ms()
        server = int(response["server_time"])
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server), server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        account = await step(errors, "account", self._client.private_futures_get_settle_accounts(dict(SETTLE)))
        raw_fees = await step(notes, "fees", self._client.private_futures_get_settle_fee({**SETTLE, "contract": FEE_REFERENCE_CONTRACT}))
        detail = await step(notes, "key_detail", self._client.private_account_get_detail())
        fees = parse(notes, "fees", parse_fee_rates, raw_fees)
        balance = parse(errors, "account", parse_balance, account)
        whitelist = detail.get("ip_whitelist") if isinstance(detail, dict) else None
        taker, maker = fees or (None, None)
        return AccountFacts(
            # Gate does not report whether a key can withdraw; it stays unknown and shows as a warning.
            permissions=KeyPermissions(ip_restricted=bool(whitelist) if whitelist is not None else None),
            one_way_position_mode=None if not isinstance(account, dict) or "in_dual_mode" not in account else not bool(account["in_dual_mode"]),
            wallet_usdt=_decimal(account.get("total")) if isinstance(account, dict) else None,
            available_usdt=balance.available_usd if balance else None,
            taker_fee_pct=taker,
            maker_fee_pct=maker,
            ping_ms=probe.ping_ms if probe else None,
            clock_offset_ms=probe.clock_offset_ms if probe else None,
            errors=tuple(errors),
            notes=tuple(notes),
        )

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        return parse_positions(await self._client.private_futures_get_settle_positions(dict(SETTLE)), instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(await self._client.private_futures_get_settle_accounts(dict(SETTLE)))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raw = await self._client.private_futures_get_settle_account_book(
            {**SETTLE, "type": "fund", "contract": symbol_raw, "from": int(since_ms / 1000), "limit": 1000}
        )
        return parse_funding(raw, symbol_raw, since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        params = {**SETTLE, "contract": instrument.symbol_raw}
        if isolated:
            params["leverage"] = str(leverage)
        else:
            params["leverage"] = "0"
            params["cross_leverage_limit"] = str(leverage)
        await self._client.private_futures_post_settle_positions_contract_leverage(params)

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
        buying = (leg is LegSide.LONG) == opening
        contracts = int(qty_units)
        params = {
            **SETTLE,
            "contract": instrument.symbol_raw,
            "size": contracts if buying else -contracts,
            "price": "0",
            "tif": "ioc",
            "text": custom_text(client_order_id),
        }
        if not opening:
            params["reduce_only"] = True
        sent = int(now_ms())
        try:
            raw = await self._client.private_futures_post_settle_orders(params)
        except Exception as exc:
            unknown = is_unknown_outcome_error(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN if unknown else OrderOutcome.REJECTED, "error", requested,
                Decimal(0), None, None, error_code(exc), describe_error("order", exc), sent, None if unknown else int(now_ms()),
            )
        return parse_order(raw, instrument, client_order_id, requested, sent, int(now_ms()))

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        sent = int(now_ms())
        text = custom_text(client_order_id)
        try:
            raw = await self._client.private_futures_get_settle_orders_order_id({**SETTLE, "order_id": text})
            return parse_order(raw, instrument, client_order_id, requested_tokens, sent, int(now_ms()))
        except Exception as exc:
            if not _not_found(exc):
                return OrderReport(
                    client_order_id, None, OrderOutcome.UNKNOWN, "error", requested_tokens, Decimal(0), None, None,
                    error_code(exc), describe_error("order_status", exc), sent, None,
                )
        # The custom id expires 60 s after the order finished: look for it among the contract's recent orders.
        try:
            for status in ("finished", "open"):
                orders = await self._client.private_futures_get_settle_orders(
                    {**SETTLE, "contract": instrument.symbol_raw, "status": status, "limit": RECENT_ORDERS_LIMIT}
                )
                match = next((order for order in orders if order.get("text") == text), None)
                if match is not None:
                    return parse_order(match, instrument, client_order_id, requested_tokens, sent, int(now_ms()))
        except Exception as exc:
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN, "error", requested_tokens, Decimal(0), None, None,
                error_code(exc), describe_error("order_status", exc), sent, None,
            )
        return OrderReport(
            client_order_id, None, OrderOutcome.REJECTED, "not_found", requested_tokens, Decimal(0), None, None,
            "ORDER_NOT_FOUND", "order not found by custom id nor among recent orders", sent, None,
        )

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        raw = await self._client.private_futures_get_settle_my_trades({**SETTLE, "contract": instrument.symbol_raw, "order": report.exchange_order_id})
        return parse_fills(raw, instrument)


def _not_found(exc: BaseException) -> bool:
    return isinstance(exc, ccxt.OrderNotFound) or "ORDER_NOT_FOUND" in str(exc)
