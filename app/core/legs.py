"""
VFP: The list of actions that brings a two-leg open or close back to a safe, hedged state after the exchanges answered.
Changes when: the policy for failed, partial or unknown leg fills changes.
Anti-goal:
1. Sending orders or reading positions here — the dispatcher executes the returned actions.
2. Ever returning a plan that leaves one leg open without its hedge and without an alert.

Decision table: docs/PLAN.md, sections 9.3 and 9.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.core.actions import (
    Action,
    AlertLevel,
    CloseLeg,
    MarkLegLost,
    MarkTradeClosed,
    MarkTradeFailed,
    MarkTradeOpen,
    QueryOrderStatus,
    RaiseAlert,
    ReduceLeg,
)
from app.core.qty import floor_to_step
from app.core.schemas import LegSide


class OrderOutcome(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LegResult:
    side: LegSide
    outcome: OrderOutcome
    filled_tokens: Decimal


def _unknown_legs(*legs: LegResult) -> list[Action]:
    unknown = [leg for leg in legs if leg.outcome is OrderOutcome.UNKNOWN]
    if not unknown:
        return []
    actions: list[Action] = [QueryOrderStatus(leg.side) for leg in unknown]
    actions.append(RaiseAlert(AlertLevel.CRITICAL, "leg_status_unknown"))
    return actions


def decide_open(
    long: LegResult,
    short: LegResult,
    requested_tokens: Decimal,
    common_step_tokens: Decimal,
    min_tokens: Decimal,
) -> list[Action]:
    """
    Decide what to do after both open orders were answered.

    min_tokens is the larger of the two exchanges' minimum quantities, in tokens.
    Unknown outcomes return status queries only; call again once statuses are resolved.
    """
    pending = _unknown_legs(long, short)
    if pending:
        return pending

    if long.filled_tokens == requested_tokens and short.filled_tokens == requested_tokens:
        return [MarkTradeOpen(requested_tokens)]

    if long.filled_tokens == 0 and short.filled_tokens == 0:
        return [MarkTradeFailed("both_rejected"), RaiseAlert(AlertLevel.WARNING, "open_rejected")]

    if long.filled_tokens == 0 or short.filled_tokens == 0:
        filled = long if long.filled_tokens > 0 else short
        return [
            CloseLeg(filled.side, filled.filled_tokens),
            MarkTradeFailed("leg_rejected"),
            RaiseAlert(AlertLevel.CRITICAL, "leg_rejected_hedge_closed"),
        ]

    target = floor_to_step(min(long.filled_tokens, short.filled_tokens), common_step_tokens)
    if target < min_tokens or target == 0:
        return [
            CloseLeg(LegSide.LONG, long.filled_tokens),
            CloseLeg(LegSide.SHORT, short.filled_tokens),
            MarkTradeFailed("partial_below_min"),
            RaiseAlert(AlertLevel.CRITICAL, "partial_fill_closed"),
        ]

    actions: list[Action] = [
        ReduceLeg(leg.side, leg.filled_tokens - target)
        for leg in (long, short)
        if leg.filled_tokens > target
    ]
    actions.append(MarkTradeOpen(target))
    actions.append(RaiseAlert(AlertLevel.WARNING, "partial_fill_equalized"))
    return actions


@dataclass(frozen=True, slots=True)
class CloseResult:
    side: LegSide
    outcome: OrderOutcome
    remaining_tokens: Decimal


def decide_close(long: CloseResult, short: CloseResult, attempt: int, max_attempts: int) -> list[Action]:
    """
    Decide what to do after a close attempt. remaining_tokens comes from the exchange position, not from our records.
    """
    pending = _unknown_legs(
        LegResult(long.side, long.outcome, Decimal(0)),
        LegResult(short.side, short.outcome, Decimal(0)),
    )
    if pending:
        return pending

    open_legs = [leg for leg in (long, short) if leg.remaining_tokens > 0]
    if not open_legs:
        return [MarkTradeClosed()]

    if attempt < max_attempts:
        return [CloseLeg(leg.side, leg.remaining_tokens) for leg in open_legs]

    actions: list[Action] = [MarkLegLost(leg.side) for leg in open_legs]
    actions.append(RaiseAlert(AlertLevel.CRITICAL, "leg_close_failed"))
    return actions
