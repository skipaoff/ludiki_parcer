"""
VFP: Canonical identity of a contract (token + multiplier) and a verdict whether two contracts are the same asset.
Changes when: exchanges introduce a new naming convention or the same-asset rules change.
Anti-goal:
1. Dropping multiplier contracts like 1000PEPE — the parser did that and lost tradable pairs.
2. Trusting the ticker alone — identical tickers on two exchanges can be different tokens.

Ported from crypto_pars/price_gap.py::normalize with three fixes:
- multiplier prefixes (1000, 10000, 1000000, 1M, Hyperliquid "k") are parsed instead of discarded;
- the Hyperliquid "k" check is case-sensitive, so KAVA or KSM are no longer dropped;
- a USDT quote is required for suffix-based exchanges, so USDC/USD contracts cannot leak in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

USDT_SUFFIXES = ("-USDT-SWAP", "USDTM", "_USDT", "-USDT", "USDT")
BARE_NAME_EXCHANGES = frozenset({"hyperliquid", "variational"})
MULTIPLIER_PREFIXES = (
    ("1000000", Decimal(1_000_000)),
    ("100000", Decimal(100_000)),
    ("10000", Decimal(10_000)),
    ("1000", Decimal(1_000)),
    ("1M", Decimal(1_000_000)),
)
TOKEN_ALIASES = {"XBT": "BTC"}


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    token: str
    multiplier: Decimal


def parse_symbol(raw: str, exchange: str) -> ParsedSymbol | None:
    """Return the canonical token and multiplier of a USDT-margined perpetual, or None if it is not one."""
    name = str(raw).strip()
    if not name or name[0] in ".@":
        return None

    multiplier = Decimal(1)
    if exchange == "hyperliquid" and len(name) > 1 and name[0] == "k" and name[1].isupper():
        name = name[1:]
        multiplier = Decimal(1_000)

    upper = name.upper()
    if exchange not in BARE_NAME_EXCHANGES:
        for suffix in USDT_SUFFIXES:
            if upper.endswith(suffix):
                upper = upper[: -len(suffix)]
                break
        else:
            return None

    for prefix, value in MULTIPLIER_PREFIXES:
        rest = upper[len(prefix):]
        if upper.startswith(prefix) and rest and rest[0].isalpha():
            upper = rest
            multiplier = value
            break

    if not upper or not upper[0].isalnum():
        return None
    return ParsedSymbol(token=TOKEN_ALIASES.get(upper, upper), multiplier=multiplier)


def price_gap_pct(price_a: Decimal, price_b: Decimal) -> Decimal:
    """Absolute gap between two per-token prices, in percent of the lower one."""
    low, high = sorted((price_a, price_b))
    if low <= 0:
        raise ValueError("prices must be positive")
    return (high - low) / low * 100


def same_asset_verdict(
    index_a: Decimal | None,
    index_b: Decimal | None,
    max_index_diff_pct: Decimal,
) -> str | None:
    """
    Decide whether two contracts track the same asset by their per-token index prices.

    Returns None when they match, otherwise a short reason code for the "suspicious" flag.
    Index prices come from spot markets, so the same token has almost identical indices on
    both exchanges even when the perpetual prices diverge — that divergence is the real gap.
    """
    if index_a is None or index_b is None or index_a <= 0 or index_b <= 0:
        return "index_missing"
    if price_gap_pct(index_a, index_b) > max_index_diff_pct:
        return "index_mismatch"
    return None
