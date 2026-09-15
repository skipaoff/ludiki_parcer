"""
VFP: Aster USDT perpetual futures behind the adapter contract — contracts, public quotes, clock probe, account check, positions, balance, funding and market orders.
Changes when: Aster changes its futures API v3 or the terminal needs more from Aster.
Anti-goal:
1. The main wallet's private key — the terminal signs with an API wallet (agent) on behalf of the main wallet address;
   a key that belongs to the main wallet itself can move funds and is refused by the account check.
2. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
3. A second copy of Binance parsing — Aster answers in Binance's shapes, so the Binance parsers are reused.

Keys: the "API key" slot holds the main wallet address (user), the "secret" slot the API wallet private key (signer).
Every private request carries user, signer and a microsecond nonce, signed as EIP-712 typed data (ccxt implements it).
Endpoints and shapes: docs/EXCHANGES.md, section Aster. Public shapes checked live on 14.09.2026, private ones follow the documentation.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import Any, Mapping

import ccxt.async_support as ccxt

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms
from app.core.legs import OrderOutcome
from app.core.pairs import Quote
from app.core.schemas import Instrument, LegSide
from app.exchanges.base import Balance, ClockProbe, FillReport, OrderReport, Position
from app.exchanges.binance.adapter import (
    ORDER_SIDES,
    parse_balance,
    parse_fee_rates,
    parse_fills,
    parse_funding,
    parse_instruments,
    parse_one_way,
    parse_order,
    parse_positions,
    parse_quotes,
    parse_usdt_balance,
    units_text,
)
from app.exchanges.ccxt_support import describe_error, error_code, is_unknown_outcome_error, now_ms, parse, step
from app.system.tls import with_shared_context

EXCHANGE = "aster"
FEE_REFERENCE_SYMBOL = "BTCUSDT"
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def valid_keys(user_address: str, signer_private_key: str) -> bool:
    return bool(ADDRESS_PATTERN.match(user_address)) and bool(PRIVATE_KEY_PATTERN.match(signer_private_key))


def _client(user_address: str | None, signer_private_key: str | None, timeout_s: float, rate_limit: bool) -> Any:
    client = with_shared_context(ccxt.aster({"privateKey": signer_private_key or "", "enableRateLimit": rate_limit, "timeout": int(timeout_s * 1000)}))
    if user_address and signer_private_key:
        # ccxt takes the "user" of a request from the signing key's own address. The terminal signs with an API wallet
        # on behalf of the main wallet, so the cached address is pinned to the main wallet and the signer set apart.
        client.options["cachedWalletAddress"] = user_address
        client.options["privateKeyHashForCachedWalletAddress"] = client.hash(client.encode(client.privateKey), "keccak", "hex")
        client.options["signerAddress"] = client.eth_get_address_from_private_key(signer_private_key)
    return client


class AsterAdapter:
    name = EXCHANGE

    def __init__(self, user_address: str | None = None, signer_private_key: str | None = None, timeout_s: float = 10) -> None:
        self._user = user_address
        self._client = _client(user_address, signer_private_key, timeout_s, rate_limit=True)
        self._probe_client = _client(None, None, timeout_s, rate_limit=False)

    @property
    def signer_address(self) -> str | None:
        return self._client.options.get("signerAddress")

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self._client.fapipublic_get_v1_exchangeinfo(), EXCHANGE)

    async def fetch_quotes(self) -> dict[str, Quote]:
        premium, books, tickers = await asyncio.gather(
            self._client.fapipublic_get_v1_premiumindex(),
            self._client.fapipublic_get_v1_ticker_bookticker(),
            self._client.fapipublic_get_v1_ticker_24hr(),
        )
        return parse_quotes(premium, books, tickers)

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._probe_client.fapipublic_get_v1_time()
        received = now_ms()
        server = int(response["serverTime"])
        return ClockProbe(ping_ms=round(received - sent), clock_offset_ms=clock_offset_ms(sent, received, server), server_ts_ms=server)

    async def check_account(self) -> AccountFacts:
        errors: list[str] = []
        notes: list[str] = []
        probe = await step(errors, "clock", self.probe_clock())
        raw_mode = await step(errors, "position_mode", self._client.fapiprivate_get_v3_positionside_dual())
        raw_balance = await step(errors, "balance", self._client.fapiprivate_get_v3_balance())
        raw_fees = await step(notes, "fees", self._client.fapiprivate_get_v3_commissionrate({"symbol": FEE_REFERENCE_SYMBOL}))
        wallet, available = parse(errors, "balance", parse_usdt_balance, raw_balance) or (None, None)
        taker, maker = parse(notes, "fees", parse_fee_rates, raw_fees) or (None, None)
        main_wallet_key = self._user is not None and self.signer_address is not None and self.signer_address.lower() == self._user.lower()
        if main_wallet_key:
            notes.append("signer: the private key belongs to the main wallet — create an API wallet and use its key instead")
        return AccountFacts(
            # An API wallet trades for the main wallet but Aster does not report its rights; the main wallet's own key can withdraw.
            permissions=KeyPermissions(withdrawals=True if main_wallet_key else None),
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
        return parse_positions(await self._client.fapiprivate_get_v3_positionrisk(), instruments, EXCHANGE)

    async def fetch_balance(self) -> Balance:
        return parse_balance(await self._client.fapiprivate_get_v3_accountwithjoinmargin(), EXCHANGE)

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        raw = await self._client.fapiprivate_get_v3_income(
            {"symbol": symbol_raw, "incomeType": "FUNDING_FEE", "startTime": since_ms, "limit": 1000}
        )
        return parse_funding(raw, since_ms)

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        await self._client.fapiprivate_post_v3_leverage({"symbol": instrument.symbol_raw, "leverage": leverage})
        try:
            await self._client.fapiprivate_post_v3_margintype(
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
            raw = await self._client.fapiprivate_post_v3_order(params)
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
            raw = await self._client.fapiprivate_get_v3_order({"symbol": instrument.symbol_raw, "origClientOrderId": client_order_id})
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
        raw = await self._client.fapiprivate_get_v3_usertrades({"symbol": instrument.symbol_raw, "orderId": report.exchange_order_id})
        return parse_fills(raw, instrument)
