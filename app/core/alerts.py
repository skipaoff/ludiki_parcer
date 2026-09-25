"""
VFP: Which gaps in the feed deserve to interrupt the user right now, and which were already announced.
Changes when: the rule for "worth a notification" changes.
Anti-goal:
1. Announcing a gap nobody can take — a row blocked by the market (thin book, stale leg, read-only venue) is silent.
2. Repeating the same gap: it is announced once, and again only after the cooldown.
3. Reading the clock or sending anything — time arrives as an argument, the caller does the announcing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Reasons that say "this account cannot trade yet", not "this gap is no good". A gap worth taking is still worth
# knowing about while trading is off — that is the state the terminal ships in.
ACCOUNT_BLOCKS = frozenset({"trading_disabled", "keys_not_accepted", "not_warmed_up", "max_open_pairs", "max_total_usd"})


@dataclass(frozen=True, slots=True)
class AlertRules:
    cooldown_ms: int = 600_000
    """The same pair is not announced again within this window, however often it leaves and re-enters the feed."""
    ignorable_blocks: frozenset[str] = ACCOUNT_BLOCKS


@dataclass(frozen=True, slots=True)
class GapAlert:
    key: str
    token: str
    long_exchange: str | None
    short_exchange: str | None
    total_pct: str | None
    profit_pct: str | None
    interest: int | None
    size_usd: str | None
    blocks: tuple[str, ...] = field(default=())
    """Account-level reasons the click is not possible yet; the gap itself is real."""


def _leg(row: Mapping[str, Any], side: str) -> str | None:
    leg = row.get(side)
    return leg.get("exchange") if isinstance(leg, Mapping) else None


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
                long_exchange=_leg(row, "long"),
                short_exchange=_leg(row, "short"),
                total_pct=row.get("total_pct"),
                profit_pct=row.get("roi_net_pct"),
                interest=row.get("score"),
                size_usd=row.get("size_usd"),
                blocks=blocks,
            )
        )
    return alerts, fresh
