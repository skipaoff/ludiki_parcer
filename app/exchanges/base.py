"""
VFP: The contract every exchange adapter implements, so the terminal never touches exchange-specific formats.
Changes when: the terminal needs a new capability from exchanges.
Anti-goal:
1. Business decisions in adapters — they translate and transport, the core decides.
2. Exchange-native units leaking out — books, positions and fills leave the adapter per token.
3. Retry or circuit-breaker logic mixed into adapter methods — resilience wraps the adapter as a separate layer.

Stage 1 implements close, probe_clock and check_account; the rest is the draft for stages 2–6
(docs/PLAN.md, section 14). Trading and private data sit on ccxt, public market streams on our own websocket clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator, Mapping, Protocol

from app.core.account import AccountFacts
from app.core.legs import OrderOutcome
from app.core.pairs import Quote
from app.core.schemas import Book, Instrument, LegSide


@dataclass(frozen=True, slots=True)
class ClockProbe:
    """One public round trip: how long it took and how far the exchange clock is from ours."""

    ping_ms: int
    clock_offset_ms: int | None
    server_ts_ms: int | None


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
class Position:
    """An open position per token: quantity in tokens, prices per token, whatever the contract's units."""

    exchange: str
    symbol_raw: str
    token: str
    side: LegSide
    qty_tokens: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    liquidation_price: Decimal | None
    leverage: int | None = None
    margin_mode: str | None = None  # isolated | cross
    updated_ms: int = 0


@dataclass(frozen=True, slots=True)
class Balance:
    exchange: str
    equity_usd: Decimal
    available_usd: Decimal
    margin_used_usd: Decimal


@dataclass(frozen=True, slots=True)
class OrderReport:
    """
    What the exchange said about one market order, per token. outcome is the terminal's reading of it:
    unknown means the order may or may not exist and its status must be queried before anything else.
    """

    client_order_id: str
    exchange_order_id: str | None
    outcome: OrderOutcome
    status: str
    requested_tokens: Decimal
    filled_tokens: Decimal
    avg_price: Decimal | None
    fee_usd: Decimal | None
    error_code: str | None
    error_message: str | None
    sent_ts_ms: int
    ack_ts_ms: int | None
    response: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FillReport:
    exchange_fill_id: str
    ts_ms: int
    price: Decimal
    qty_tokens: Decimal
    fee: Decimal
    fee_asset: str
    is_maker: bool | None


class ExchangeAdapter(Protocol):
    name: str

    async def close(self) -> None: ...

    async def probe_clock(self) -> ClockProbe:
        """Public, no keys needed."""
        ...

    async def check_account(self) -> AccountFacts:
        """Needs keys. Every step that fails is recorded in AccountFacts.errors instead of raising."""
        ...

    async def load_instruments(self) -> list[Instrument]:
        """Public. Tradable USDT perpetuals with their unit bridge to tokens."""
        ...

    async def fetch_quotes(self) -> dict[str, Quote]:
        """Public. Best prices, mark, index and 24h turnover of every contract, keyed by raw symbol."""
        ...

    def watch_top_of_book(self, symbols_raw: list[str]) -> AsyncIterator[TopOfBook]: ...

    def watch_book(self, symbol_raw: str) -> AsyncIterator[Book]: ...

    def watch_mark_index(self) -> AsyncIterator[MarkIndex]: ...

    async def fetch_volume_24h_usd(self) -> dict[str, Decimal]: ...

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        """Needs keys. Set leverage and margin mode for a contract; already-set values are not an error."""
        ...

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
        """
        Needs keys. Opening buys a long or sells a short; closing is reduce-only in the opposite direction.
        Never raises for exchange or network errors — they come back as a rejected or unknown outcome.
        """
        ...

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        """Needs keys. Current state of an order by the terminal's own id; an order the exchange never saw is rejected."""
        ...

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        """Needs keys. Executions of a filled order with their fees."""
        ...

    async def fetch_positions(self, instruments: Mapping[str, Instrument]) -> list[Position]:
        """Needs keys. Non-zero positions of contracts found in instruments (keyed by raw symbol), per token."""
        ...

    async def fetch_balance(self) -> Balance:
        """Needs keys. USDT futures account totals."""
        ...

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        """Needs keys. Net funding received (positive) or paid (negative) on a contract since a moment."""
        ...

    def watch_positions(self) -> AsyncIterator[Position]: ...

    def watch_orders(self) -> AsyncIterator[OrderReport]: ...
