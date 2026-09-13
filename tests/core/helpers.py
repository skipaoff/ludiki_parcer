"""VFP: Compact builders for books, instruments and fees used across core tests."""

from __future__ import annotations

from decimal import Decimal

from app.core.schemas import Book, Fees, Instrument, Level


def D(value: str | int) -> Decimal:
    return Decimal(str(value))


def levels(*pairs: tuple[str, str]) -> tuple[Level, ...]:
    return tuple(Level(price=D(price), qty_tokens=D(qty)) for price, qty in pairs)


def book(exchange: str, bids: tuple[Level, ...] = (), asks: tuple[Level, ...] = ()) -> Book:
    return Book(exchange=exchange, bids=bids, asks=asks, exchange_ts_ms=0, received_ts_ms=0)


def fees(long_pct: str = "0.05", short_pct: str = "0.05") -> Fees:
    return Fees(taker_long_pct=D(long_pct), taker_short_pct=D(short_pct))


def instrument(
    exchange: str,
    symbol_raw: str,
    token: str,
    qty_unit_tokens: str = "1",
    price_unit_tokens: str = "1",
    qty_step_units: str = "1",
    min_qty_units: str = "1",
    max_market_qty_units: str | None = None,
    min_notional_usd: str = "5",
) -> Instrument:
    return Instrument(
        exchange=exchange,
        symbol_raw=symbol_raw,
        token=token,
        qty_unit_tokens=D(qty_unit_tokens),
        price_unit_tokens=D(price_unit_tokens),
        qty_step_units=D(qty_step_units),
        min_qty_units=D(min_qty_units),
        max_market_qty_units=D(max_market_qty_units) if max_market_qty_units is not None else None,
        min_notional_usd=D(min_notional_usd),
    )
