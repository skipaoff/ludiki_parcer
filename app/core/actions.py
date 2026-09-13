"""
VFP: Typed, serializable intents that core decisions return and the execution dispatcher carries out.
Changes when: the execution layer learns a new kind of effect.
Anti-goal:
1. Live clients, sessions or callbacks inside an action — actions are plain data, loggable and replayable.
2. Deciding anything here — the decision lives in core modules, the effect in the dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.core.schemas import LegSide


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class MarkTradeOpen:
    qty_tokens: Decimal


@dataclass(frozen=True, slots=True)
class MarkTradeFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class MarkTradeClosed:
    pass


@dataclass(frozen=True, slots=True)
class MarkLegLost:
    side: LegSide


@dataclass(frozen=True, slots=True)
class ReduceLeg:
    """Reduce-only market order that shrinks an open leg by qty_tokens."""

    side: LegSide
    qty_tokens: Decimal


@dataclass(frozen=True, slots=True)
class CloseLeg:
    """Reduce-only market order that closes qty_tokens of a leg completely."""

    side: LegSide
    qty_tokens: Decimal


@dataclass(frozen=True, slots=True)
class QueryOrderStatus:
    side: LegSide


@dataclass(frozen=True, slots=True)
class RaiseAlert:
    level: AlertLevel
    code: str


Action = (
    MarkTradeOpen
    | MarkTradeFailed
    | MarkTradeClosed
    | MarkLegLost
    | ReduceLeg
    | CloseLeg
    | QueryOrderStatus
    | RaiseAlert
)
