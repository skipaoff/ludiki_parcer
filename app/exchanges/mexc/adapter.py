"""
VFP: MEXC futures behind the adapter contract — contracts, public quotes, clock probe, account and key check.
Changes when: MEXC changes its contract API or the terminal needs more from MEXC.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Guessing key permissions — MEXC does not report them for futures keys, so they stay unknown.

Endpoints and response shapes: docs/EXCHANGES.md, section MEXC. ccxt 4.5.78 uses https://api.mexc.com/api/v1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms
from app.core.pairs import Quote
from app.core.schemas import Instrument
from app.core.symbols import parse_symbol
from app.exchanges.base import ClockProbe
from app.exchanges.ccxt_support import now_ms, parse, step

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
        self._client = ccxt.mexc(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
                "options": {"defaultType": "swap"},
            }
        )
        # Probes get their own client so the rate limiter never queues them behind catalog requests.
        self._probe_client = ccxt.mexc({"enableRateLimit": False, "timeout": int(timeout_s * 1000)})

    async def close(self) -> None:
        await self._client.close()
        await self._probe_client.close()

    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self._client.contract_public_get_detail())

    async def fetch_quotes(self) -> dict[str, Quote]:
        return parse_quotes(await self._client.contract_public_get_ticker())

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
