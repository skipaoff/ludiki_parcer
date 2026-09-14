"""
VFP: Whether a pair may be opened right now, with every reason it may not — decided from memory in microseconds before any order leaves.
Changes when: pre-trade checks or risk limits change (PLAN.md, sections 9.2 and 9.5).
Anti-goal:
1. Order-sending or network calls — every fact arrives as an argument.
2. A soft "maybe" — any failed check blocks; the caller shows all reasons at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_open_pairs: int
    max_total_usd: Decimal
    one_pair_per_token: bool
    margin_buffer_pct: Decimal
    entry_min_roi_pct: Decimal


@dataclass(frozen=True, slots=True)
class OpenFacts:
    trading_enabled: bool
    token: str
    tradable_pair: bool
    both_legs_fresh: bool
    roi_net_pct: Decimal | None
    qty_problem: str | None
    notional_usd: Decimal
    available_long_usd: Decimal | None
    available_short_usd: Decimal | None
    leverage_long: int
    leverage_short: int
    open_pairs: int
    open_tokens: frozenset[str]
    open_notional_usd: Decimal
    keys_accepted: tuple[bool, bool]
    exchange_blocked: tuple[str | None, str | None]
    warmed_up: bool
    busy: bool


def open_blocks(facts: OpenFacts, limits: RiskLimits) -> list[str]:
    reasons: list[str] = []
    if not facts.trading_enabled:
        reasons.append("trading_disabled")
    if facts.busy:
        reasons.append("pair_busy")
    if not all(facts.keys_accepted):
        reasons.append("keys_not_accepted")
    for blocked in facts.exchange_blocked:
        if blocked:
            reasons.append(f"exchange_blocked:{blocked}")
    if not facts.tradable_pair:
        reasons.append("pair_not_tradable")
    if not facts.both_legs_fresh:
        reasons.append("stale")
    if facts.qty_problem:
        reasons.append(facts.qty_problem)
    if facts.roi_net_pct is None:
        reasons.append("no_quote")
    elif facts.roi_net_pct < limits.entry_min_roi_pct:
        reasons.append("roi_below_entry")
    if facts.open_pairs >= limits.max_open_pairs:
        reasons.append("max_open_pairs")
    if facts.open_notional_usd + facts.notional_usd > limits.max_total_usd:
        reasons.append("max_total_usd")
    if limits.one_pair_per_token and facts.token in facts.open_tokens:
        reasons.append("token_already_open")
    buffer = 1 + limits.margin_buffer_pct / 100
    for side, available, leverage in (
        ("long", facts.available_long_usd, facts.leverage_long),
        ("short", facts.available_short_usd, facts.leverage_short),
    ):
        if available is None:
            reasons.append(f"balance_unknown:{side}")
        elif leverage <= 0 or available < facts.notional_usd / leverage * buffer:
            reasons.append(f"insufficient_margin:{side}")
    if not facts.warmed_up:
        reasons.append("not_warmed_up")
    return reasons


def client_order_id(trade_id: int, purpose: str, attempt: int = 0) -> str:
    """
    Unique per order and readable back to its trade: 'lk' + trade id in base 36 + purpose code + attempt.
    Fits both Binance (36 chars, [.A-Z:/a-z0-9_-]) and MEXC external order ids.
    """
    codes = {"open_long": "ol", "open_short": "os", "close_long": "cl", "close_short": "cs", "fix_leg": "fx"}
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    value, encoded = trade_id, ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = digits[remainder] + encoded
    return f"lk{encoded or '0'}{codes[purpose]}{attempt}"
