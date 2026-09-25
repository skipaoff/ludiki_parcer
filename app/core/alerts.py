"""
VFP: Which gaps in the feed deserve to interrupt the user right now, and which were already announced.
Changes when: the rule for "worth a notification" changes.
Anti-goal:
1. Announcing a gap nobody can take — a row blocked by the market (thin book, stale leg, read-only venue) is silent.
2. Repeating the same gap: it is announced once, and again only after the cooldown.
3. Reading the clock or sending anything — time arrives as an argument, the caller does the announcing.
4. Recomputing anything: an alert carries the row's own numbers, so the message and the screen cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

# Reasons that say "this account cannot trade yet", not "this gap is no good". A gap worth taking is still worth
# knowing about while trading is off — that is the state the terminal ships in, and without keys every row also
# carries balance_unknown (checked live on macOS 25.09.2026: without it nothing would ever be announced).
ACCOUNT_BLOCKS = frozenset(
    {"trading_disabled", "keys_not_accepted", "not_warmed_up", "max_open_pairs", "max_total_usd", "balance_unknown"}
)


@dataclass(frozen=True, slots=True)
class AlertRules:
    cooldown_ms: int = 600_000
    """The same pair is not announced again within this window, however often it leaves and re-enters the feed."""
    ignorable_blocks: frozenset[str] = ACCOUNT_BLOCKS


@dataclass(frozen=True, slots=True)
class AlertLeg:
    exchange: str | None
    symbol: str | None = None
    url: str | None = None
    price: str | None = None


@dataclass(frozen=True, slots=True)
class GapAlert:
    key: str
    token: str
    long: AlertLeg
    short: AlertLeg
    total_pct: str | None
    profit_pct: str | None
    profit_usd: str | None
    interest: int | None
    size_usd: str | None
    capacity_usd: str | None = None
    funding_horizon_pct: str | None = None
    funding_next_pct: str | None = None
    funding_next_ms: int | None = None
    volume24h_weak_usd: str | None = None
    lifetime_ms: int | None = None
    blocks: tuple[str, ...] = field(default=())
    """Account-level reasons the click is not possible yet; the gap itself is real."""

    @property
    def long_exchange(self) -> str | None:
        return self.long.exchange

    @property
    def short_exchange(self) -> str | None:
        return self.short.exchange


def _leg(row: Mapping[str, Any], side: str) -> AlertLeg:
    leg = row.get(side)
    if not isinstance(leg, Mapping):
        return AlertLeg(exchange=None)
    return AlertLeg(
        exchange=leg.get("exchange"),
        symbol=leg.get("symbol"),
        url=leg.get("url"),
        price=row.get(f"{side}_price"),
    )


def _funding(row: Mapping[str, Any], name: str) -> Any:
    funding = row.get("funding")
    return funding.get(name) if isinstance(funding, Mapping) else None


def decide(
    rows: Iterable[Mapping[str, Any]],
    announced: Mapping[str, int],
    now_ms: int,
    rules: AlertRules = AlertRules(),
) -> tuple[list[GapAlert], dict[str, int]]:
    """Feed rows in, alerts out, plus the new "already announced" map the caller keeps for the next round."""
    fresh = {key: ts for key, ts in announced.items() if now_ms - ts < rules.cooldown_ms * 2}
    alerts: list[GapAlert] = []
    for row in rows:
        blocks = tuple(str(block) for block in (row.get("open_blocks") or []))
        market_blocks = [block for block in blocks if block.split(":", 1)[0] not in rules.ignorable_blocks]
        if market_blocks or row.get("block"):
            continue
        key = str(row.get("key"))
        last = fresh.get(key)
        if last is not None and now_ms - last < rules.cooldown_ms:
            continue
        fresh[key] = now_ms
        alerts.append(
            GapAlert(
                key=key,
                token=str(row.get("token")),
                long=_leg(row, "long"),
                short=_leg(row, "short"),
                total_pct=row.get("total_pct"),
                profit_pct=row.get("roi_net_pct"),
                profit_usd=row.get("profit_usd"),
                interest=row.get("score"),
                size_usd=row.get("size_usd"),
                capacity_usd=row.get("capacity_usd"),
                funding_horizon_pct=_funding(row, "horizon_pct"),
                funding_next_pct=_funding(row, "next_pct"),
                funding_next_ms=_funding(row, "next_ms"),
                volume24h_weak_usd=row.get("volume24h_weak_usd"),
                lifetime_ms=row.get("lifetime_ms"),
                blocks=blocks,
            )
        )
    return alerts, fresh


@dataclass(frozen=True, slots=True)
class LiveAlert:
    """An announced gap while it is still on screen: enough to say later how it ended."""

    key: str
    announced_ms: int
    peak_total_pct: str | None = None
    missing_since_ms: int | None = None


@dataclass(frozen=True, slots=True)
class FinishedAlert:
    key: str
    lifetime_ms: int
    peak_total_pct: str | None


def _better(current: str | None, candidate: Any) -> str | None:
    if candidate in (None, ""):
        return current
    if current is None:
        return str(candidate)
    try:
        return str(candidate) if Decimal(str(candidate)) > Decimal(current) else current
    except InvalidOperation:
        return current


def track(
    live: Mapping[str, LiveAlert],
    rows: Iterable[Mapping[str, Any]],
    now_ms: int,
    gone_after_ms: int = 60_000,
) -> tuple[dict[str, LiveAlert], list[FinishedAlert]]:
    """
    Follow announced gaps: keep their best result while they are in the feed, and report the ones that left it.

    A gap blinking out for a tick is not over; only an absence longer than gone_after_ms ends the story.
    """
    present = {str(row.get("key")): row for row in rows}
    updated: dict[str, LiveAlert] = {}
    finished: list[FinishedAlert] = []
    for key, entry in live.items():
        row = present.get(key)
        if row is not None:
            updated[key] = LiveAlert(
                key=key,
                announced_ms=entry.announced_ms,
                peak_total_pct=_better(entry.peak_total_pct, row.get("total_pct")),
                missing_since_ms=None,
            )
            continue
        missing_since = entry.missing_since_ms if entry.missing_since_ms is not None else now_ms
        if now_ms - missing_since >= gone_after_ms:
            finished.append(
                FinishedAlert(key=key, lifetime_ms=missing_since - entry.announced_ms, peak_total_pct=entry.peak_total_pct)
            )
            continue
        updated[key] = LiveAlert(key, entry.announced_ms, entry.peak_total_pct, missing_since)
    return updated, finished


def passes(alert: GapAlert, min_interest: int = 0, min_total_pct: Decimal | None = None) -> bool:
    """A second, stricter gate for a channel that reaches a phone: not every feed row is worth a buzz."""
    if min_interest and (alert.interest is None or alert.interest < min_interest):
        return False
    if min_total_pct is not None:
        total = None if alert.total_pct in (None, "") else Decimal(str(alert.total_pct))
        if total is None or total < min_total_pct:
            return False
    return True
