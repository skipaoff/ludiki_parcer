"""
VFP: Cheap first pass over every pair — best-price ROI in both directions — and the choice of which pairs deserve live order books.
Changes when: candidate selection, subscription priorities or the subscription budget rules change (PLAN.md, section 5.2).
Anti-goal:
1. Using best-price ROI to size or trade — it is only an upper bound; book-based ROI decides.
2. Subscription thrash — a subscribed pair keeps its book for a minimum hold time unless something more important needs the slot.
3. Decimal here — this runs for every pair several times a second, floats are fine for an upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TopRoi:
    """
    gross_pct — price difference short minus long at best prices, in % of the long price, before fees:
    the same measure as the book entry spread and the exit spread, so the three can be compared directly.
    """

    key: str
    long_exchange: str
    short_exchange: str
    roi_net_pct: float
    gross_pct: float = 0.0


def best_price_roi(
    key: str,
    exchange_a: str,
    bid_a: float,
    ask_a: float,
    exchange_b: str,
    bid_b: float,
    ask_b: float,
    round_trip_fee_pct: float,
) -> TopRoi | None:
    """Per-token best prices of both legs; the direction with the higher net ROI. None when any price is missing."""
    if min(bid_a, ask_a, bid_b, ask_b) <= 0:
        return None
    a_gross = (bid_b - ask_a) / ask_a * 100
    b_gross = (bid_a - ask_b) / ask_b * 100
    if a_gross >= b_gross:
        return TopRoi(key, exchange_a, exchange_b, a_gross - round_trip_fee_pct, a_gross)
    return TopRoi(key, exchange_b, exchange_a, b_gross - round_trip_fee_pct, b_gross)


@dataclass(frozen=True, slots=True)
class TopCheck:
    """The exchange's independently delivered best prices of the same contract, per token."""

    bid: float
    ask: float


PRICE_TOLERANCE = 1e-9


def _same_price(a: float, b: float) -> bool:
    return abs(a - b) <= PRICE_TOLERANCE * max(abs(a), abs(b), 1e-18)


def leg_fresh(
    book_age_ms: float | None,
    book_bid: float | None,
    book_ask: float | None,
    top: TopCheck | None,
    fresh_ms: int,
    quiet_book_max_ms: int,
) -> bool:
    """
    A book is fresh when it changed recently. A quiet book (exchanges push books only on change) is still trusted for up
    to quiet_book_max_ms, but only while the contract's own best prices — delivered separately — match it exactly.
    Prices that differ mean one of the two sources stopped updating, and there is no telling which: not fresh.
    A live connection alone proves nothing: it carries other contracts too (13.09.2026, a Binance book of ALT stood
    still for 4.5 s during a pump while frames of other contracts kept the connection busy).
    """
    if book_age_ms is None:
        return False
    if book_age_ms <= fresh_ms:
        return True
    if book_age_ms > quiet_book_max_ms or top is None or book_bid is None or book_ask is None:
        return False
    return _same_price(top.bid, book_bid) and _same_price(top.ask, book_ask)


def choose_books(
    must_keep: list[str],
    ranked: list[str],
    current: dict[str, int],
    now_ms: int,
    limit: int,
    min_hold_ms: int,
) -> dict[str, int]:
    """
    Pick at most limit pair keys to hold order books for, returning key -> subscribed-since timestamp.

    must_keep — pairs that must have books (open pairs, gaps in or tracked by the feed), in priority order.
    ranked — remaining candidates, best first.
    current — what is subscribed now with its subscription time; young subscriptions are kept over newcomers.
    """
    chosen: dict[str, int] = {}

    def take(key: str) -> None:
        if len(chosen) < limit and key not in chosen:
            chosen[key] = current.get(key, now_ms)

    for key in must_keep:
        take(key)
    for key, since in sorted(current.items(), key=lambda item: item[1]):
        if now_ms - since < min_hold_ms:
            take(key)
    for key in ranked:
        take(key)
    return chosen
