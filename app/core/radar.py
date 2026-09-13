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
    key: str
    long_exchange: str
    short_exchange: str
    roi_net_pct: float


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
    a_long = (bid_b - ask_a) / ask_a * 100 - round_trip_fee_pct
    b_long = (bid_a - ask_b) / ask_b * 100 - round_trip_fee_pct
    if a_long >= b_long:
        return TopRoi(key, exchange_a, exchange_b, a_long)
    return TopRoi(key, exchange_b, exchange_a, b_long)


def leg_fresh(book_age_ms: float | None, stream_age_ms: float | None, fresh_ms: int, quiet_book_max_ms: int) -> bool:
    """
    A book is fresh when it changed recently, or when it is quiet but its connection is demonstrably alive:
    exchanges push order books only on change, so a still book on a live connection is still the book.
    """
    if book_age_ms is None:
        return False
    if book_age_ms <= fresh_ms:
        return True
    return stream_age_ms is not None and stream_age_ms <= fresh_ms and book_age_ms <= quiet_book_max_ms


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
