"""
VFP: Whether an exchange account and its API key are fit for the terminal, and why not — from facts an adapter collected.
Changes when: the rules for an acceptable key or account change (PLAN.md, sections 9.1 and 12).
Anti-goal:
1. Accepting a key that can withdraw funds — that is always a blocking problem, never a warning.
2. Treating unknown as fine — facts the exchange did not report become warnings, not silent passes.
3. Calling exchanges or reading the clock here — facts and timestamps arrive as arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

CLOCK_WARNING_MS = 1000


@dataclass(frozen=True, slots=True)
class KeyPermissions:
    reading: bool | None = None
    futures: bool | None = None
    withdrawals: bool | None = None
    ip_restricted: bool | None = None


@dataclass(frozen=True, slots=True)
class AccountFacts:
    """What one check of an exchange account found. None means the exchange did not tell us or the call failed."""

    permissions: KeyPermissions = KeyPermissions()
    one_way_position_mode: bool | None = None
    wallet_usdt: Decimal | None = None
    available_usdt: Decimal | None = None
    taker_fee_pct: Decimal | None = None
    maker_fee_pct: Decimal | None = None
    ping_ms: int | None = None
    clock_offset_ms: int | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    """errors: steps whose failure makes the check incomplete. notes: optional steps that failed."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """blocking: trading on this exchange is refused. warnings: allowed, but shown to the user."""

    blocking: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.blocking


def clock_offset_ms(sent_ms: float, received_ms: float, server_ms: int) -> int:
    """Server clock minus local clock, assuming the server stamped the reply halfway through the round trip."""
    return round(server_ms - (sent_ms + received_ms) / 2)


def judge(facts: AccountFacts, clock_warning_ms: int = CLOCK_WARNING_MS) -> Verdict:
    blocking: list[str] = []
    warnings: list[str] = []
    permissions = facts.permissions

    if facts.errors:
        blocking.append("check_incomplete")
    if permissions.withdrawals is True:
        blocking.append("withdrawals_enabled")
    elif permissions.withdrawals is None:
        warnings.append("withdrawals_unknown")
    if permissions.futures is False:
        blocking.append("futures_disabled")
    if permissions.reading is False:
        blocking.append("reading_disabled")
    if facts.one_way_position_mode is False:
        blocking.append("hedge_mode")
    elif facts.one_way_position_mode is None:
        warnings.append("position_mode_unknown")
    if permissions.ip_restricted is False:
        warnings.append("no_ip_restriction")
    if facts.available_usdt is not None and facts.available_usdt <= 0:
        warnings.append("no_free_balance")
    if facts.taker_fee_pct is None:
        warnings.append("fees_unknown")
    if facts.clock_offset_ms is not None and abs(facts.clock_offset_ms) > clock_warning_ms:
        warnings.append("clock_offset")

    return Verdict(tuple(blocking), tuple(warnings))
