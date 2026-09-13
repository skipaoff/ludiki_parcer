"""
VFP: Immutable data shapes shared by every pure function in the core.
Changes when: the terminal's internal market or trade vocabulary changes.
Anti-goal:
1. Behaviour or validation logic here — shapes only, math lives in sibling modules.
2. Exchange-native units (contracts, "1000PEPE" lots) — everything here is per token.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class LegSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class Level:
    """One order-book level already converted to price per token and quantity in tokens."""

    price: Decimal
    qty_tokens: Decimal


@dataclass(frozen=True, slots=True)
class Book:
    """Order book of one contract. Bids are sorted best-first (descending), asks best-first (ascending)."""

    exchange: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    exchange_ts_ms: int
    received_ts_ms: int


@dataclass(frozen=True, slots=True)
class Instrument:
    """
    Trading rules of one perpetual contract, with the unit bridge to tokens.

    qty_unit_tokens   — tokens represented by one exchange quantity unit
                        (Binance BTCUSDT: 1, Binance 1000PEPEUSDT: 1000, MEXC BTC_USDT contract: 0.0001).
    price_unit_tokens — tokens the exchange price is quoted for
                        (Binance 1000PEPEUSDT: 1000, MEXC PEPE_USDT: 1).
    """

    exchange: str
    symbol_raw: str
    token: str
    qty_unit_tokens: Decimal
    price_unit_tokens: Decimal
    qty_step_units: Decimal
    min_qty_units: Decimal
    max_market_qty_units: Decimal | None
    min_notional_usd: Decimal

    @property
    def qty_step_tokens(self) -> Decimal:
        return self.qty_step_units * self.qty_unit_tokens

    @property
    def min_qty_tokens(self) -> Decimal:
        return self.min_qty_units * self.qty_unit_tokens

    @property
    def max_market_qty_tokens(self) -> Decimal | None:
        if self.max_market_qty_units is None:
            return None
        return self.max_market_qty_units * self.qty_unit_tokens


@dataclass(frozen=True, slots=True)
class Fees:
    """Taker fees in percent of notional for each leg."""

    taker_long_pct: Decimal
    taker_short_pct: Decimal

    @property
    def round_trip_pct(self) -> Decimal:
        """Entry and exit on both legs: four taker fills."""
        return (self.taker_long_pct + self.taker_short_pct) * 2
