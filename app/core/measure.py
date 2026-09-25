"""
VFP: The decision numbers of one gap — funding over the horizon, expected result and interest — in one value.
Changes when: the definition of the expected result or of interest changes.
Anti-goal:
1. Two versions of the same arithmetic — the feed row and the history row must never disagree about a gap.
2. Treating unknown funding as zero: funding is None until both legs report a rate (core/funding.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.funding import PairFunding
from app.core.interest import Interest, interest


@dataclass(frozen=True, slots=True)
class Measurement:
    funding: PairFunding | None
    total_pct: Decimal | None
    """Book profit after fees plus funding over the horizon: what the gap is expected to leave."""
    score: Interest | None

    @property
    def interest(self) -> int | None:
        return None if self.score is None else self.score.score


def measure(
    profit_pct: Decimal | None,
    funding: PairFunding | None,
    capacity_usd: Decimal | None,
    size_usd: Decimal,
    lifetime_ms: int | None,
    volume24h_weak_usd: Decimal | None,
    suspicious: bool,
    *,
    scored: bool = True,
) -> Measurement:
    """
    Combine the book profit with funding and score the result.

    `scored` is False for a row the terminal refuses to trust (a stale leg): such a row keeps its numbers but
    gets no interest score, because scoring it would rank a number nobody may act on.
    """
    total_pct = None if profit_pct is None else profit_pct + (funding.horizon_pct if funding else Decimal(0))
    score = (
        interest(total_pct, capacity_usd, size_usd, lifetime_ms, volume24h_weak_usd, suspicious) if scored else None
    )
    return Measurement(funding=funding, total_pct=total_pct, score=score)
