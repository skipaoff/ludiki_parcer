"""
VFP: Binance USDⓈ-M futures behind the adapter contract — public clock probe and the account and key check.
Changes when: Binance changes these endpoints or the terminal needs more from Binance.
Anti-goal:
1. Deciding whether the key is acceptable — the adapter reports facts, app/core/account.py judges them.
2. Floats for money — balances and fee rates are parsed from Binance's decimal strings.

Endpoints and response shapes: docs/EXCHANGES.md, section Binance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms
from app.exchanges.base import ClockProbe
from app.exchanges.ccxt_support import now_ms, parse, step, to_bool

FEE_REFERENCE_SYMBOL = "BTCUSDT"


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
        self._client = ccxt.binanceusdm(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                "enableRateLimit": True,
                "timeout": int(timeout_s * 1000),
            }
        )
        if demo:
            self._client.enable_demo_trading(True)

    async def close(self) -> None:
        await self._client.close()

    async def probe_clock(self) -> ClockProbe:
        sent = now_ms()
        response = await self._client.fapipublic_get_time()
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
