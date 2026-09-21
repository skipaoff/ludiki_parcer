"""
VFP: One order quantity in tokens that is valid on both exchanges, plus its exchange-native units.
Changes when: the rules for sizing a two-leg position change.
Anti-goal:
1. Sizing the legs in dollars — both legs must hold exactly the same number of tokens.
2. Float arithmetic — steps like 0.001 must stay exact, so everything is Decimal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.core.schemas import Instrument


@dataclass(frozen=True, slots=True)
class QtyPlan:
    qty_tokens: Decimal
    units_long: Decimal
    units_short: Decimal


@dataclass(frozen=True, slots=True)
class QtyRejected:
    reason: str


def _scale(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -int(exponent))


def decimal_lcm(a: Decimal, b: Decimal) -> Decimal:
    """Least common multiple of two positive decimal steps, e.g. lcm(0.5, 0.3) = 1.5."""
    if a <= 0 or b <= 0:
        raise ValueError("steps must be positive")
    scale = max(_scale(a), _scale(b))
    factor = Decimal(10) ** scale
    lcm_int = math.lcm(int(a * factor), int(b * factor))
    return (Decimal(lcm_int) / factor).normalize()


def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    steps = (qty / step).to_integral_value(rounding=ROUND_FLOOR)
    return (steps * step).normalize()


def common_step_tokens(long: Instrument, short: Instrument) -> Decimal:
    return decimal_lcm(long.qty_step_tokens, short.qty_step_tokens)


def to_units(qty_tokens: Decimal, instrument: Instrument) -> Decimal:
    """Exchange-native quantity; raises if the token amount is not a whole multiple of the exchange step."""
    units = qty_tokens / instrument.qty_unit_tokens
    if (units / instrument.qty_step_units) % 1 != 0:
        raise ValueError(f"{qty_tokens} tokens is not a multiple of the {instrument.exchange} step")
    return units.normalize()


def _limit_violation(qty_tokens: Decimal, price_per_token: Decimal, instrument: Instrument) -> str | None:
    if qty_tokens < instrument.min_qty_tokens:
        return f"below_min_qty:{instrument.exchange}"
    if qty_tokens * price_per_token < instrument.min_notional_usd:
        return f"below_min_notional:{instrument.exchange}"
    max_market = instrument.max_market_qty_tokens
    if max_market is not None and qty_tokens > max_market:
        return f"above_max_market_qty:{instrument.exchange}"
    return None


def plan_quantity(
    size_usd: Decimal,
    price_per_token: Decimal,
    long: Instrument,
    short: Instrument,
    step: Decimal | None = None,
) -> QtyPlan | QtyRejected:
    """Convert the configured dollar size into one token quantity valid on both legs; step — their common step, if known."""
    if size_usd <= 0 or price_per_token <= 0:
        return QtyRejected("non_positive_input")
    step = common_step_tokens(long, short) if step is None else step
    qty = floor_to_step(size_usd / price_per_token, step)
    if qty <= 0:
        return QtyRejected("size_below_common_step")
    for instrument in (long, short):
        violation = _limit_violation(qty, price_per_token, instrument)
        if violation:
            return QtyRejected(violation)
    return QtyPlan(qty_tokens=qty, units_long=to_units(qty, long), units_short=to_units(qty, short))
