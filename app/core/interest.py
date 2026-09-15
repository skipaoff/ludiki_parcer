"""
VFP: How interesting a gap is to open, as one number from 0 to 100 with the parts it is made of.
Changes when: the weighting of result, depth, stability and liquidity is revised.
Anti-goal:
1. A score for a gap that loses money — an expected result at or below zero, or a suspicious pair, scores 0.
2. Hidden weighting — every part is returned so the screen can show why a gap scored what it did.

Parts (maximum points):
- result, 50 — expected result (spread after fees and book, plus funding over the horizon) in % of the size; 2% or more is full;
- depth, 20 — how many sizes the books carry while the gap stays above the feed threshold; 3 sizes or more is full;
- stability, 15 — how long the gap has held; 5 minutes or more is full;
- liquidity, 15 — 24h turnover of the weaker leg, on a log scale from $100k (none) to $10M (full).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

FULL_RESULT_PCT = Decimal("2")
FULL_DEPTH_SIZES = Decimal("3")
FULL_STABILITY_MS = 300_000
LIQUIDITY_FLOOR_USD = 100_000
LIQUIDITY_FULL_USD = 10_000_000


@dataclass(frozen=True, slots=True)
class Interest:
    score: int
    result: int
    depth: int
    stability: int
    liquidity: int


def _share(value: float) -> float:
    return max(0.0, min(1.0, value))


def interest(
    total_pct: Decimal | None,
    capacity_usd: Decimal | None,
    size_usd: Decimal,
    lifetime_ms: int | None,
    volume24h_weak_usd: Decimal | None,
    suspicious: bool,
) -> Interest | None:
    """None when the gap has no book measurement yet."""
    if total_pct is None:
        return None
    if suspicious or total_pct <= 0:
        return Interest(0, 0, 0, 0, 0)
    result = round(50 * _share(float(total_pct / FULL_RESULT_PCT)))
    depth = round(20 * _share(float(capacity_usd / size_usd / FULL_DEPTH_SIZES))) if capacity_usd and size_usd > 0 else 0
    stability = round(15 * _share((lifetime_ms or 0) / FULL_STABILITY_MS))
    if volume24h_weak_usd and volume24h_weak_usd > 0:
        span = math.log10(LIQUIDITY_FULL_USD / LIQUIDITY_FLOOR_USD)
        liquidity = round(15 * _share(math.log10(float(volume24h_weak_usd) / LIQUIDITY_FLOOR_USD) / span))
    else:
        liquidity = 0
    return Interest(result + depth + stability + liquidity, result, depth, stability, liquidity)
