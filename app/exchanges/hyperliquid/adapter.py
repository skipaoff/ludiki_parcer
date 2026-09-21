"""
VFP: Hyperliquid perpetuals behind the adapter contract — coins, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: Hyperliquid changes its info or exchange endpoints, or the terminal needs more from it.
Anti-goal:
1. The main wallet's private key — orders are signed by an API wallet approved for the main wallet, which cannot
   withdraw; a key whose address is the main wallet itself is refused by the account check.
2. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
3. Pretending a market order exists — Hyperliquid has only limit orders; a market order is an immediate-or-cancel limit
   at the best opposite price moved by MARKET_SLIPPAGE, so it fills now or not at all.

Quotes are in USDC, which the terminal compares with USDT prices of other exchanges as equal.
Keys: the "API key" slot holds the main wallet address, the "secret" slot the API wallet private key.
Endpoints and shapes: docs/EXCHANGES.md, section Hyperliquid. Public shapes checked live on 15.09.2026, private ones follow the documentation.
"""

from __future__ import annotations

import hashlib
import re
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

EXCHANGE = "hyperliquid"
MIN_ORDER_USD = Decimal("10")
MARKET_SLIPPAGE = "0.02"
FILLS_LOOKBACK_MS = 120_000
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def valid_keys(wallet_address: str, agent_private_key: str) -> bool:
    return bool(ADDRESS_PATTERN.match(wallet_address)) and bool(PRIVATE_KEY_PATTERN.match(agent_private_key))


def cloid(client_order_id: str) -> str:
    """Hyperliquid client ids are 128-bit hex strings; the terminal's id maps to one deterministically."""
    return "0x" + hashlib.md5(client_order_id.encode()).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def parse_instruments(meta_and_contexts: list[Any]) -> list[Instrument]:
    """
    POST /info {"type": "metaAndAssetCtxs"} — [meta, contexts]. Listed coins that are not delisted; szDecimals is the size
    step; every order must be worth at least $10; prices keep at most 5 significant figures, so there is no fixed tick.
    kPEPE is a lot of 1000 PEPE, priced per lot.
    """
    meta = meta_and_contexts[0]
    instruments = []
    for coin in meta.get("universe") or []:
        name = str(coin.get("name") or "")
        if coin.get("isDelisted") or ":" in name:
            continue
        parsed = parse_symbol(name, EXCHANGE)
        if parsed is None or coin.get("szDecimals") is None:
            continue
        step_units = Decimal(1).scaleb(-int(coin["szDecimals"]))
        instruments.append(
            Instrument(
                exchange=EXCHANGE,
                symbol_raw=name,
                token=parsed.token,
                qty_unit_tokens=parsed.multiplier,
                price_unit_tokens=parsed.multiplier,
                qty_step_units=step_units,
                min_qty_units=step_units,
                max_market_qty_units=None,
                min_notional_usd=MIN_ORDER_USD,
                price_tick=None,
            )
        )
    return instruments


def parse_quotes(meta_and_contexts: list[Any]) -> dict[str, Quote]:
    """
    Contexts line up with the universe: impactPxs (bid and ask at the impact size — the best prices, or slightly worse
    in thin books), markPx, oraclePx (the index), dayNtlVlm (24h notional in USDC).
    """
    meta, contexts = meta_and_contexts[0], meta_and_contexts[1]
    quotes = {}
    for coin, context in zip(meta.get("universe") or [], contexts):
        impact = context.get("impactPxs") or [None, None]
        quotes[coin["name"]] = Quote(
            bid=_positive(impact[0]),
            ask=_positive(impact[1]),
            mark=_positive(context.get("markPx")),
            index=_positive(context.get("oraclePx")),
            volume24h_usd=_positive(context.get("dayNtlVlm")),
        )
    return quotes


def parse_positions(state: dict[str, Any], instruments: Mapping[str, Instrument]) -> list[Position]:
    """
    POST /info {"type": "clearinghouseState", "user"} — assetPositions[].position: coin, szi (signed size), entryPx,
    positionValue, liquidationPx, leverage {type cross/isolated, value}.
    """
    positions = []
    for entry in state.get("assetPositions") or []:
        item = entry.get("position") or {}
        instrument = instruments.get(item.get("coin", ""))
        size = _decimal(item.get("szi")) or Decimal(0)
        if instrument is None or size == 0:
            continue
        unit_price = instrument.price_unit_tokens
        value = _positive(item.get("positionValue"))
        liquidation = _positive(item.get("liquidationPx"))
        leverage = item.get("leverage") or {}
        positions.append(
            Position(
                exchange=EXCHANGE,
                symbol_raw=item["coin"],
                token=instrument.token,
                side=LegSide.LONG if size > 0 else LegSide.SHORT,
                qty_tokens=abs(size) * instrument.qty_unit_tokens,
                entry_price=(_decimal(item.get("entryPx")) or Decimal(0)) / unit_price,
                mark_price=value / abs(size) / unit_price if value else None,
                liquidation_price=liquidation / unit_price if liquidation else None,
                leverage=int(leverage["value"]) if leverage.get("value") else None,
                margin_mode="isolated" if leverage.get("type") == "isolated" else "cross" if leverage.get("type") == "cross" else None,
                updated_ms=int(state.get("time") or 0),
            )
        )
    return positions


def parse_balance(state: dict[str, Any]) -> Balance:
    """clearinghouseState: marginSummary.accountValue and totalMarginUsed, withdrawable (free to open with)."""
    summary = state.get("marginSummary") or {}
    return Balance(
        exchange=EXCHANGE,
        equity_usd=_decimal(summary.get("accountValue")) or Decimal(0),
        available_usd=_decimal(state.get("withdrawable")) or Decimal(0),
        margin_used_usd=_decimal(summary.get("totalMarginUsed")) or Decimal(0),
    )


def parse_funding(entries: list[dict[str, Any]], coin: str, since_ms: int) -> Decimal:
    """POST /info {"type": "userFunding"} — delta.usdc per hourly payment: positive received, negative paid."""
    return sum(
        (
            _decimal((item.get("delta") or {}).get("usdc")) or Decimal(0)
            for item in entries
            if (item.get("delta") or {}).get("coin") == coin and int(item.get("time") or 0) >= since_ms
        ),
        Decimal(0),
    )


def parse_fills(entries: list[dict[str, Any]], instrument: Instrument, order_id: str) -> list[FillReport]:
    """POST /info {"type": "userFillsByTime"} — px, sz, fee (USDC), tid, oid, time, crossed (true = taker)."""
    return [
        FillReport(
            exchange_fill_id=str(item["tid"]),
            ts_ms=int(item.get("time") or 0),
            price=(_decimal(item.get("px")) or Decimal(0)) / instrument.price_unit_tokens,
            qty_tokens=(_decimal(item.get("sz")) or Decimal(0)) * instrument.qty_unit_tokens,
            fee=abs(_decimal(item.get("fee")) or Decimal(0)),
            fee_asset=str(item.get("feeToken") or "USDC"),
            is_maker=None if item.get("crossed") is None else not item.get("crossed"),
        )
        for item in entries
        if str(item.get("oid")) == order_id and item.get("coin") == instrument.symbol_raw
    ]


def average_price(fills: list[FillReport]) -> Decimal | None:
    total = sum((fill.qty_tokens for fill in fills), Decimal(0))
    return sum((fill.price * fill.qty_tokens for fill in fills), Decimal(0)) / total if total > 0 else None


def parse_submit(status: dict[str, Any], instrument: Instrument, client_order_id: str, requested: Decimal, sent: int, ack: int) -> OrderReport:
    """The order status of an exchange answer: {"filled": {totalSz, avgPx, oid}}, {"resting": {oid}} or {"error": text}."""
    if "filled" in status:
        filled = status["filled"]
        tokens = (_decimal(filled.get("totalSz")) or Decimal(0)) * instrument.qty_unit_tokens
        average = _positive(filled.get("avgPx"))
        return OrderReport(
            client_order_id, str(filled.get("oid")), OrderOutcome.FILLED if tokens >= requested else OrderOutcome.PARTIAL, "filled",
            requested, tokens, average / instrument.price_unit_tokens if average else None, None, None, None, sent, ack, status,
        )
    if "error" in status:
        return OrderReport(client_order_id, None, OrderOutcome.REJECTED, "error", requested, Decimal(0), None, None, "rejected",
                           str(status["error"])[:300], sent, ack, status)
    resting = status.get("resting") or {}
    return OrderReport(client_order_id, str(resting.get("oid")) if resting.get("oid") else None, OrderOutcome.UNKNOWN, "resting",
                       requested, Decimal(0), None, None, None, None, sent, ack, status)


def parse_order_status(answer: dict[str, Any], instrument: Instrument, client_order_id: str, requested: Decimal, sent: int) -> OrderReport:
    """
    POST /info {"type": "orderStatus", "oid": cloid} — {"status": "order", "order": {"order": {oid, origSz, sz}, "status":
    filled/open/canceled/rejected/…}} or {"status": "unknownOid"}.
    """
    if answer.get("status") != "order":
        return OrderReport(client_order_id, None, OrderOutcome.REJECTED, "not_found", requested, Decimal(0), None, None,
                           "unknownOid", "no order with this client id", sent, None, answer)
    wrapper = answer.get("order") or {}
    order = wrapper.get("order") or {}
    state = str(wrapper.get("status") or "")
    filled = ((_decimal(order.get("origSz")) or Decimal(0)) - (_decimal(order.get("sz")) or Decimal(0))) * instrument.qty_unit_tokens
    if state == "open":
        outcome = OrderOutcome.UNKNOWN
    elif filled >= requested:
        outcome = OrderOutcome.FILLED
    elif filled > 0:
        outcome = OrderOutcome.PARTIAL
    else:
        outcome = OrderOutcome.REJECTED
    return OrderReport(client_order_id, str(order["oid"]) if order.get("oid") is not None else None, outcome, state, requested, filled,
                       None, None, None if outcome is not OrderOutcome.REJECTED else state, None, sent, int(now_ms()), answer)


class HyperliquidAdapter:
    name = EXCHANGE

    def __init__(self, wallet_address: str | None = None, agent_private_key: str | None = None, timeout_s: float = 10) -> None:
        self._wallet = wallet_address
        config = {"walletAddress": wallet_address or "", "privateKey": agent_private_key or "", "enableRateLimit": True,
                  "timeout": int(timeout_s * 1000)}
        self._client = with_shared_context(ccxt.hyperliquid(config))
        self._probe_client = with_shared_context(ccxt.hyperliquid({"enableRateLimit": False, "timeout": int(timeout_s * 1000)}))
        self._symbols: dict[str, str] = {}

    @property
    def agent_address(self) -> str | None:
        key = self._client.privateKey
        return self._client.eth_get_address_from_private_key(key) if key else None

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def _info(self, request: dict[str, Any]) -> Any:
        return await self._client.public_post_info(request)

    async def _symbol(self, coin: str) -> str:
        """The ccxt market symbol of a coin, for the signed actions ccxt builds (orders, leverage)."""
        if coin not in self._symbols:
            await self._client.load_markets()
            self._symbols = {market.get("baseName"): symbol for symbol, market in self._client.markets.items() if market.get("swap")}
        return self._symbols[coin]

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self._info({"type": "metaAndAssetCtxs"}))

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(await self._info({"type": "metaAndAssetCtxs"}))

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.public_post_info({"type": "l2Book", "coin": "BTC"})
        received = now_ms()
        server = int(response.get("time") or 0) or None
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server) if server else None, server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        state = await step(errors, "balance", self._info({"type": "clearinghouseState", "user": self._wallet}))
        agents = await step(errors, "api_wallet", self._info({"type": "extraAgents", "user": self._wallet}))
        fees = await step(notes, "fees", self._info({"type": "userFees", "user": self._wallet}))
        agent = (self.agent_address or "").lower()
        main_wallet_key = bool(agent) and agent == (self._wallet or "").lower()
        approved = None
        if isinstance(agents, list):
            approved = any(str(item.get("address", "")).lower() == agent for item in agents)
            if not approved and not main_wallet_key:
                errors.append("api_wallet: this key is not an API wallet approved for the wallet address")
        if main_wallet_key:
            notes.append("signer: the private key belongs to the main wallet — create an API wallet and use its key instead")
        balance = parse(errors, "balance", parse_balance, state)
        taker = maker = None
        if isinstance(fees, dict) and fees.get("userCrossRate") is not None:
            taker, maker = Decimal(str(fees["userCrossRate"])) * 100, Decimal(str(fees.get("userAddRate") or "0")) * 100
        return AccountFacts(
            # API wallets trade but cannot withdraw; the main wallet's own key can.
            permissions=KeyPermissions(
                reading=True, futures=approved, withdrawals=True if main_wallet_key else (False if approved else None), ip_restricted=None
            ),
            one_way_position_mode=True,  # Hyperliquid keeps one position per coin
            wallet_usdt=balance.equity_usd if balance else None,
            available_usdt=balance.available_usd if balance else None,
            taker_fee_pct=taker,
            maker_fee_pct=maker,
            ping_ms=probe.ping_ms if probe else None,
            clock_offset_ms=probe.clock_offset_ms if probe else None,
            errors=tuple(errors),
            notes=tuple(notes),
        )

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        return parse_positions(await self._info({"type": "clearinghouseState", "user": self._wallet}), instruments)

    async def fetch_balance(self) -> Balance:
        return parse_balance(await self._info({"type": "clearinghouseState", "user": self._wallet}))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        return parse_funding(await self._info({"type": "userFunding", "user": self._wallet, "startTime": since_ms}) or [], symbol_raw, since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        await self._client.set_leverage(leverage, await self._symbol(instrument.symbol_raw), {"marginMode": "isolated" if isolated else "cross"})

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
        sent = int(now_ms())
        try:
            book = await self._info({"type": "l2Book", "coin": instrument.symbol_raw})
            levels = book["levels"][1 if buying else 0]
            price = levels[0]["px"]
            order = await self._client.create_order(
                await self._symbol(instrument.symbol_raw),
                "market",
                "buy" if buying else "sell",
                format(qty_units.normalize(), "f"),
                price,
                {"clientOrderId": cloid(client_order_id), "reduceOnly": not opening, "slippage": MARKET_SLIPPAGE},
            )
        except Exception as exc:
            unknown = is_unknown_outcome_error(exc)
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN if unknown else OrderOutcome.REJECTED, "error", requested,
                Decimal(0), None, None, error_code(exc), describe_error("order", exc), sent, None if unknown else int(now_ms()),
            )
        return parse_submit(order.get("info") or {}, instrument, client_order_id, requested, sent, int(now_ms()))

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        sent = int(now_ms())
        try:
            answer = await self._info({"type": "orderStatus", "user": self._wallet, "oid": cloid(client_order_id)})
            report = parse_order_status(answer, instrument, client_order_id, requested_tokens, sent)
            if report.filled_tokens > 0 and report.exchange_order_id:
                fills = await self.fetch_fills(instrument, report)
                average = average_price(fills)
                if average is not None:
                    report = replace(report, avg_price=average)
            return report
        except Exception as exc:
            return OrderReport(
                client_order_id, None, OrderOutcome.UNKNOWN, "error", requested_tokens, Decimal(0), None, None,
                error_code(exc), describe_error("order_status", exc), sent, None,
            )

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.exchange_order_id is None:
            return []
        entries = await self._info({"type": "userFillsByTime", "user": self._wallet, "startTime": (report.sent_ts_ms or int(now_ms())) - FILLS_LOOKBACK_MS})
        return parse_fills(entries or [], instrument, report.exchange_order_id)
