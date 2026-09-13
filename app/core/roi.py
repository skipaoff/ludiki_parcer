"""
VFP: Book-based economics of a two-leg gap trade — entry ROI, capacity, exit spread, PnL now, liquidation distance.
Changes when: the definition of what a gap trade earns or costs changes.
Anti-goal:
1. Reading exchanges, clocks or settings here — every input arrives as an argument.
2. ROI from top-of-book for sizing decisions — top-of-book is only an upper bound for candidate selection.

Formulas are specified in docs/PLAN.md, section 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.core.schemas import Book, Fees, Level
from app.core.vwap import depth_tokens, walk


@dataclass(frozen=True, slots=True)
class EntryQuote:
    long_exchange: str
    short_exchange: str
    qty_tokens: Decimal
    long_avg: Decimal
    short_avg: Decimal
    roi_gross_pct: Decimal
    roi_net_pct: Decimal


@dataclass(frozen=True, slots=True)
class ExitQuote:
    qty_tokens: Decimal
    long_exit_avg: Decimal
    short_exit_avg: Decimal
    exit_spread_pct: Decimal


def gross_pct(buy_price: Decimal, sell_price: Decimal) -> Decimal:
    return (sell_price - buy_price) / buy_price * 100


def entry_quote(long_book: Book, short_book: Book, qty_tokens: Decimal, fees: Fees) -> EntryQuote | None:
    """Buy qty on the long book's asks, sell it on the short book's bids. None when either side is too thin."""
    buy = walk(long_book.asks, qty_tokens)
    sell = walk(short_book.bids, qty_tokens)
    if buy is None or sell is None:
        return None
    gross = gross_pct(buy.avg_price, sell.avg_price)
    return EntryQuote(
        long_exchange=long_book.exchange,
        short_exchange=short_book.exchange,
        qty_tokens=qty_tokens,
        long_avg=buy.avg_price,
        short_avg=sell.avg_price,
        roi_gross_pct=gross,
        roi_net_pct=gross - fees.round_trip_pct,
    )


def best_entry(book_a: Book, book_b: Book, qty_tokens: Decimal, fees_ab: Fees, fees_ba: Fees) -> EntryQuote | None:
    """
    Try both directions and return the one with the higher net ROI.

    fees_ab — fees when book_a is the long leg; fees_ba — when book_b is the long leg.
    """
    quotes = [
        quote
        for quote in (
            entry_quote(book_a, book_b, qty_tokens, fees_ab),
            entry_quote(book_b, book_a, qty_tokens, fees_ba),
        )
        if quote is not None
    ]
    return max(quotes, key=lambda quote: quote.roi_net_pct, default=None)


def top_of_book_roi_net(long_book: Book, short_book: Book, fees: Fees) -> Decimal | None:
    """Upper bound of net ROI for any size. A pair below the threshold here can never pass by depth."""
    if not long_book.asks or not short_book.bids:
        return None
    return gross_pct(long_book.asks[0].price, short_book.bids[0].price) - fees.round_trip_pct


def capacity_tokens(
    long_asks: Sequence[Level],
    short_bids: Sequence[Level],
    fees: Fees,
    min_roi_net_pct: Decimal,
    step_tokens: Decimal,
) -> Decimal:
    """
    Largest multiple of step_tokens whose book-based net ROI is still at or above the threshold.

    Net ROI only worsens as size grows, so a binary search over step multiples is exact.
    Returns 0 when even one step does not pass.
    """
    visible = min(depth_tokens(long_asks), depth_tokens(short_bids))
    high = int(visible / step_tokens)
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        qty = step_tokens * middle
        buy = walk(long_asks, qty)
        sell = walk(short_bids, qty)
        passes = (
            buy is not None
            and sell is not None
            and gross_pct(buy.avg_price, sell.avg_price) - fees.round_trip_pct >= min_roi_net_pct
        )
        if passes:
            low = middle
        else:
            high = middle - 1
    return step_tokens * low


def exit_quote(long_book: Book, short_book: Book, qty_tokens: Decimal) -> ExitQuote | None:
    """Close now: sell the long on its bids, buy back the short on its asks."""
    sell_long = walk(long_book.bids, qty_tokens)
    buy_short = walk(short_book.asks, qty_tokens)
    if sell_long is None or buy_short is None:
        return None
    spread = (buy_short.avg_price - sell_long.avg_price) / sell_long.avg_price * 100
    return ExitQuote(
        qty_tokens=qty_tokens,
        long_exit_avg=sell_long.avg_price,
        short_exit_avg=buy_short.avg_price,
        exit_spread_pct=spread,
    )


def exit_fees_usd(exit: ExitQuote, fees: Fees) -> Decimal:
    return (
        exit.qty_tokens * exit.long_exit_avg * fees.taker_long_pct
        + exit.qty_tokens * exit.short_exit_avg * fees.taker_short_pct
    ) / 100


def pnl_now_usd(
    entry_long_avg: Decimal,
    entry_short_avg: Decimal,
    exit: ExitQuote,
    fees: Fees,
    entry_fees_paid_usd: Decimal,
    funding_net_usd: Decimal,
) -> Decimal:
    """PnL if both legs were closed at market right now, after all fees and funding."""
    long_leg = exit.qty_tokens * (exit.long_exit_avg - entry_long_avg)
    short_leg = exit.qty_tokens * (entry_short_avg - exit.short_exit_avg)
    return long_leg + short_leg - entry_fees_paid_usd - exit_fees_usd(exit, fees) + funding_net_usd


def liquidation_distance_pct(mark_price: Decimal, liquidation_price: Decimal | None) -> Decimal | None:
    if liquidation_price is None or liquidation_price <= 0 or mark_price <= 0:
        return None
    return abs(mark_price - liquidation_price) / mark_price * 100
