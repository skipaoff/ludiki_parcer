"""
VFP: How exchange positions line up with the terminal's trades — which legs belong to which pair, what is foreign, what went missing — and what an open pair is worth right now.
Changes when: the reconciliation rules or the per-pair metrics change (PLAN.md, section 10).
Anti-goal:
1. Trusting the database over the exchange — quantities and liquidation come from positions, records only say what belongs together.
2. Silently absorbing a mismatch — a missing leg or a different quantity is always reported as an issue.
3. Reading exchanges, books or the clock — everything arrives as arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.qty import floor_to_step
from app.core.roi import ExitQuote, liquidation_distance_pct, pnl_now_usd
from app.core.schemas import Fees, LegSide
from app.exchanges.base import Position


@dataclass(frozen=True, slots=True)
class TradeLegs:
    trade_id: int
    token: str
    long_exchange: str
    long_symbol: str
    short_exchange: str
    short_symbol: str
    qty_tokens: Decimal


@dataclass(frozen=True, slots=True)
class Reconciliation:
    legs: dict[int, tuple[Position | None, Position | None]]
    foreign: list[Position]
    issues: dict[int, tuple[str, ...]] = field(default_factory=dict)


def reconcile(trades: list[TradeLegs], positions: list[Position]) -> Reconciliation:
    by_key = {(position.exchange, position.symbol_raw, position.side): position for position in positions}
    attached: set[tuple[str, str, LegSide]] = set()
    legs: dict[int, tuple[Position | None, Position | None]] = {}
    issues: dict[int, tuple[str, ...]] = {}
    for trade in trades:
        long_key = (trade.long_exchange, trade.long_symbol, LegSide.LONG)
        short_key = (trade.short_exchange, trade.short_symbol, LegSide.SHORT)
        long, short = by_key.get(long_key), by_key.get(short_key)
        found: list[str] = []
        for name, key, position in (("long", long_key, long), ("short", short_key, short)):
            if position is None:
                found.append(f"{name}_missing")
                continue
            attached.add(key)
            if position.qty_tokens != trade.qty_tokens:
                found.append(f"{name}_qty_mismatch")
        legs[trade.trade_id] = (long, short)
        if found:
            issues[trade.trade_id] = tuple(found)
    foreign = [
        position for position in positions if (position.exchange, position.symbol_raw, position.side) not in attached
    ]
    return Reconciliation(legs, foreign, issues)


def suggest_pairs(foreign: list[Position]) -> list[tuple[Position, Position]]:
    """Foreign positions that form a hedged pair: same token, long on one exchange and short on another."""
    suggestions = []
    longs = [position for position in foreign if position.side is LegSide.LONG]
    shorts = [position for position in foreign if position.side is LegSide.SHORT]
    for long in longs:
        for short in shorts:
            if short.token == long.token and short.exchange != long.exchange:
                suggestions.append((long, short))
    return sorted(suggestions, key=lambda pair: (pair[0].token, pair[0].exchange))


def pair_quantity(long: Position, short: Position, common_step_tokens: Decimal) -> Decimal:
    """The hedged quantity two legs share, on the step both exchanges accept."""
    return floor_to_step(min(long.qty_tokens, short.qty_tokens), common_step_tokens)


def entry_spread_pct(entry_long: Decimal, entry_short: Decimal) -> Decimal:
    return (entry_short - entry_long) / entry_long * 100


def estimated_entry_fees_usd(qty: Decimal, entry_long: Decimal, entry_short: Decimal, fees: Fees) -> Decimal:
    """Taker fees of opening both legs; used until real fills are recorded (stage 6)."""
    return qty * (entry_long * fees.taker_long_pct + entry_short * fees.taker_short_pct) / 100


@dataclass(frozen=True, slots=True)
class PairMetrics:
    entry_spread_pct: Decimal
    exit_spread_pct: Decimal | None
    pnl_now_usd: Decimal | None
    liq_distance_long_pct: Decimal | None
    liq_distance_short_pct: Decimal | None

    @property
    def worst_liquidation_pct(self) -> Decimal | None:
        values = [value for value in (self.liq_distance_long_pct, self.liq_distance_short_pct) if value is not None]
        return min(values) if values else None


def pair_metrics(
    entry_long: Decimal,
    entry_short: Decimal,
    exit: ExitQuote | None,
    fees: Fees,
    entry_fees_usd: Decimal,
    funding_usd: Decimal,
    mark_long: Decimal | None,
    liquidation_long: Decimal | None,
    mark_short: Decimal | None,
    liquidation_short: Decimal | None,
) -> PairMetrics:
    return PairMetrics(
        entry_spread_pct=entry_spread_pct(entry_long, entry_short),
        exit_spread_pct=exit.exit_spread_pct if exit else None,
        pnl_now_usd=pnl_now_usd(entry_long, entry_short, exit, fees, entry_fees_usd, funding_usd) if exit else None,
        liq_distance_long_pct=liquidation_distance_pct(mark_long, liquidation_long) if mark_long else None,
        liq_distance_short_pct=liquidation_distance_pct(mark_short, liquidation_short) if mark_short else None,
    )
