"""
VFP: What funding does to a pair — each leg's rate with its own interval, the payments that fall inside a holding horizon, and the soonest payment.
Changes when: how funding is projected for a pair changes.
Anti-goal:
1. Averaging scheduled payments — a leg pays only at its settlement times, so a pair opened a minute before settlement pays
   that settlement in full, and one opened just after pays nothing until the next.
2. Treating an unknown rate as zero — a leg without a rate makes the pair's funding unknown, not free.
3. Exchange formats here — rates arrive already as percent per settlement.

Sign convention (all exchanges): a positive rate means longs pay shorts. For a pair that is long on one exchange and short
on the other, the long leg receives −rate and the short leg receives +rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class FundingRate:
    """rate_pct — percent of notional per settlement; next_ms is None for venues that accrue without a schedule."""

    rate_pct: Decimal
    interval_hours: Decimal
    next_ms: int | None = None

    @property
    def interval_ms(self) -> int:
        return int(self.interval_hours * HOUR_MS)

    @property
    def hourly_pct(self) -> Decimal:
        return self.rate_pct / self.interval_hours


def next_settlement_ms(rate: FundingRate, now_ms: int) -> int | None:
    """The first settlement strictly after now; a stale next time is rolled forward by whole intervals."""
    if rate.next_ms is None or rate.interval_ms <= 0:
        return rate.next_ms
    if rate.next_ms > now_ms:
        return rate.next_ms
    return rate.next_ms + ((now_ms - rate.next_ms) // rate.interval_ms + 1) * rate.interval_ms


def settlements_within(rate: FundingRate, now_ms: int, horizon_ms: int) -> Decimal:
    """Settlements in (now, now + horizon]; a venue without a schedule accrues in proportion to time."""
    if rate.interval_ms <= 0:
        return Decimal(0)
    following = next_settlement_ms(rate, now_ms)
    if following is None:
        return Decimal(horizon_ms) / Decimal(rate.interval_ms)
    end = now_ms + horizon_ms
    if following > end:
        return Decimal(0)
    return Decimal(1 + (end - following) // rate.interval_ms)


@dataclass(frozen=True, slots=True)
class PairFunding:
    """Percent of notional the pair receives (positive) or pays (negative)."""

    hourly_pct: Decimal
    horizon_pct: Decimal
    next_ms: int | None
    next_pct: Decimal | None
    changes_ms: int | None = None  # until this moment the same rates give the same result; None — never


def _changes_ms(rate: FundingRate, now_ms: int, horizon_ms: int) -> int | None:
    """When the settlements inside (now, now + horizon] next change: the first one passes, or the next one comes in."""
    following = next_settlement_ms(rate, now_ms)
    if following is None or rate.interval_ms <= 0:
        return None
    entering = next_settlement_ms(rate, now_ms + horizon_ms)
    return following if entering is None else min(following, entering - horizon_ms)


def pair_funding(long: FundingRate | None, short: FundingRate | None, now_ms: int, horizon_hours: Decimal) -> PairFunding | None:
    if long is None or short is None:
        return None
    horizon_ms = int(horizon_hours * HOUR_MS)
    legs = ((-long.rate_pct, long), (short.rate_pct, short))
    horizon = sum((per_settlement * settlements_within(rate, now_ms, horizon_ms) for per_settlement, rate in legs), Decimal(0))
    upcoming = [(next_settlement_ms(rate, now_ms), per_settlement) for per_settlement, rate in legs]
    scheduled = [(moment, amount) for moment, amount in upcoming if moment is not None]
    next_ms = min((moment for moment, _ in scheduled), default=None)
    next_pct = sum((amount for moment, amount in scheduled if moment == next_ms), Decimal(0)) if next_ms is not None else None
    return PairFunding(
        hourly_pct=short.hourly_pct - long.hourly_pct,
        horizon_pct=horizon,
        next_ms=next_ms,
        next_pct=next_pct,
        changes_ms=min((moment for moment in (_changes_ms(long, now_ms, horizon_ms), _changes_ms(short, now_ms, horizon_ms)) if moment is not None), default=None),
    )
