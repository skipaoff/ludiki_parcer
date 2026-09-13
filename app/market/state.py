"""
VFP: The latest best prices and order books per contract, already converted to price per token and quantity in tokens, with their age.
Changes when: the terminal keeps new kinds of market data in memory.
Anti-goal:
1. Exchange units leaking to consumers — every book leaving here is per token, whatever the contract multiplier.
2. Hiding staleness — every entry carries when the exchange stamped it and when it arrived here.
3. Network or parsing of exchange formats — stream clients hand over already-parsed prices.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from app.core.schemas import Book, Instrument, Level

Key = tuple[str, str]  # (exchange, raw symbol)


@dataclass(slots=True)
class Top:
    """Best prices per token as floats: an upper-bound radar, never used for sizing."""

    bid: float
    ask: float
    exchange_ts_ms: int
    received_ms: float


@dataclass(slots=True)
class Mark:
    """Mark and index price per token, and the 24h turnover when the source provides it."""

    mark: float | None
    index: float | None
    volume24h_usd: float | None
    received_ms: float


def per_token_levels(raw: Sequence[Sequence[str | float]], instrument: Instrument, descending: bool) -> tuple[Level, ...]:
    """Raw [price, quantity in exchange units, ...] levels into per-token Levels, best first, zero quantities dropped."""
    price_unit = instrument.price_unit_tokens
    qty_unit = instrument.qty_unit_tokens
    levels = [
        Level(price=Decimal(str(level[0])) / price_unit, qty_tokens=Decimal(str(level[1])) * qty_unit)
        for level in raw
        if Decimal(str(level[1])) > 0
    ]
    levels.sort(key=lambda level: level.price, reverse=descending)
    return tuple(levels)


class MarketState:
    def __init__(self, clock_ms: Callable[[], float] = lambda: time.time() * 1000) -> None:
        self._clock_ms = clock_ms
        self._instruments: dict[Key, Instrument] = {}
        self.tops: dict[Key, Top] = {}
        self.books: dict[Key, Book] = {}
        self.marks: dict[Key, Mark] = {}
        self.messages: dict[str, int] = {}

    def set_instruments(self, instruments: Sequence[Instrument]) -> None:
        self._instruments = {(item.exchange, item.symbol_raw): item for item in instruments}

    def instrument(self, key: Key) -> Instrument | None:
        return self._instruments.get(key)

    def count(self, exchange: str, messages: int = 1) -> None:
        self.messages[exchange] = self.messages.get(exchange, 0) + messages

    def set_top(self, exchange: str, symbol: str, bid: float, ask: float, exchange_ts_ms: int) -> None:
        instrument = self._instruments.get((exchange, symbol))
        if instrument is None:
            return
        unit = float(instrument.price_unit_tokens)
        self.tops[(exchange, symbol)] = Top(bid / unit, ask / unit, exchange_ts_ms, self._clock_ms())

    def set_mark(
        self, exchange: str, symbol: str, mark: float | None, index: float | None, volume24h_usd: float | None = None
    ) -> None:
        instrument = self._instruments.get((exchange, symbol))
        if instrument is None:
            return
        unit = float(instrument.price_unit_tokens)
        previous = self.marks.get((exchange, symbol))
        self.marks[(exchange, symbol)] = Mark(
            mark=mark / unit if mark else None,
            index=index / unit if index else None,
            volume24h_usd=volume24h_usd if volume24h_usd is not None else (previous.volume24h_usd if previous else None),
            received_ms=self._clock_ms(),
        )

    def set_book(
        self,
        exchange: str,
        symbol: str,
        raw_bids: Sequence[Sequence[str | float]],
        raw_asks: Sequence[Sequence[str | float]],
        exchange_ts_ms: int,
    ) -> None:
        instrument = self._instruments.get((exchange, symbol))
        if instrument is None:
            return
        self.books[(exchange, symbol)] = Book(
            exchange=exchange,
            bids=per_token_levels(raw_bids, instrument, descending=True),
            asks=per_token_levels(raw_asks, instrument, descending=False),
            exchange_ts_ms=exchange_ts_ms,
            received_ts_ms=int(self._clock_ms()),
        )

    def drop_book(self, key: Key) -> None:
        self.books.pop(key, None)

    def book_age_ms(self, key: Key) -> float | None:
        book = self.books.get(key)
        return None if book is None else self._clock_ms() - book.received_ts_ms

    def top_age_ms(self, key: Key) -> float | None:
        top = self.tops.get(key)
        return None if top is None else self._clock_ms() - top.received_ms
