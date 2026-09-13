"""
VFP: The contract every exchange adapter implements, so the terminal never touches exchange-specific formats.
Changes when: the terminal needs a new capability from exchanges.
Anti-goal:
1. Business decisions in adapters — they translate and transport, the core decides.
2. Exchange-native units leaking out — books, positions and fills leave the adapter per token.
3. Retry or circuit-breaker logic mixed into adapter methods — resilience wraps the adapter as a separate layer.

Draft for stage 1 (docs/PLAN.md, section 14). Implementations will sit on ccxt, with direct
websocket connections where ccxt is slower than needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import AsyncIterator, Protocol

from app.core.schemas import Book, Instrument, LegSide


@dataclass(frozen=True, slots=True)
class TopOfBook:
    symbol_raw: str
    bid: Decimal
    ask: Decimal
    exchange_ts_ms: int
    received_ts_ms: int


@dataclass(frozen=True, slots=True)
class MarkIndex:
    symbol_raw: str
    mark_price: Decimal
    index_price: Decimal | None
    ts_ms: int


@dataclass(frozen=True, slots=True)
class AccountCheck:
    balance_usdt: Decimal
    withdrawals_enabled: bool | None
    one_way_position_mode: bool
    taker_fee_pct: Decimal
    ping_ms: int
    clock_offset_ms: int


@dataclass(frozen=True, slots=True)
class Position:
    symbol_raw: str
    side: LegSide
    qty_tokens: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None


@dataclass(frozen=True, slots=True)
class OrderReport:
    client_order_id: str
    exchange_order_id: str | None
    status: str
    filled_tokens: Decimal
    avg_price: Decimal | None
    fee_usd: Decimal | None
    error_code: str | None
    sent_ts_ms: int
    ack_ts_ms: int | None


class ExchangeAdapter(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def check_account(self) -> AccountCheck: ...

    async def load_instruments(self) -> list[Instrument]: ...

    def watch_top_of_book(self, symbols_raw: list[str]) -> AsyncIterator[TopOfBook]: ...

    def watch_book(self, symbol_raw: str) -> AsyncIterator[Book]: ...

    def watch_mark_index(self) -> AsyncIterator[MarkIndex]: ...

    async def fetch_volume_24h_usd(self) -> dict[str, Decimal]: ...

    async def prepare_symbol(self, symbol_raw: str, leverage: int, isolated: bool) -> None: ...

    async def place_market_order(
        self,
        symbol_raw: str,
        side: LegSide,
        qty_units: Decimal,
        reduce_only: bool,
        client_order_id: str,
    ) -> OrderReport: ...

    async def fetch_order(self, symbol_raw: str, client_order_id: str) -> OrderReport: ...

    async def fetch_positions(self) -> list[Position]: ...

    def watch_positions(self) -> AsyncIterator[Position]: ...

    def watch_orders(self) -> AsyncIterator[OrderReport]: ...

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal: ...
