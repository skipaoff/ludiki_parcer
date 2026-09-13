"""
VFP: The average price a market order of a given token quantity would actually get from an order book side.
Changes when: the way we walk the book changes (e.g. fee tiers per level or hidden liquidity).
Anti-goal:
1. Using last or mark price as the fill price — a thin book turns a paper gap into an instant loss.
2. Silently filling a partial quantity — insufficient depth must be an explicit None.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.core.schemas import Level


@dataclass(frozen=True, slots=True)
class Fill:
    qty_tokens: Decimal
    avg_price: Decimal
    worst_price: Decimal
    levels_used: int


def depth_tokens(levels: Sequence[Level]) -> Decimal:
    return sum((level.qty_tokens for level in levels), Decimal(0))


def walk(levels: Sequence[Level], qty_tokens: Decimal) -> Fill | None:
    """Walk best-first levels until qty_tokens is filled. None when the visible book is too thin."""
    if qty_tokens <= 0:
        raise ValueError("quantity must be positive")
    remaining = qty_tokens
    cost = Decimal(0)
    for used, level in enumerate(levels, start=1):
        take = min(remaining, level.qty_tokens)
        cost += take * level.price
        remaining -= take
        if remaining == 0:
            return Fill(qty_tokens=qty_tokens, avg_price=cost / qty_tokens, worst_price=level.price, levels_used=used)
    return None
