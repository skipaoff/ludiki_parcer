"""
VFP: Which contracts on two exchanges form a tradable pair, and whether the pair is trustworthy — from instruments and public quotes.
Changes when: the rules for matching contracts or flagging a pair as suspicious change (PLAN.md, section 6).
Anti-goal:
1. Comparing prices in exchange units — 1000PEPEUSDT and PEPE_USDT only compare per token.
2. A missing quote or index passing as "same asset" — missing data is a reason, not a pass.
3. Reading exchanges or storage — instruments and quotes arrive as arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.qty import common_step_tokens
from app.core.schemas import Instrument
from app.core.symbols import price_gap_pct, same_asset_verdict

MAX_PRICE_GAP_PCT = Decimal("20")
MAX_INDEX_GAP_PCT = Decimal("1")


@dataclass(frozen=True, slots=True)
class Quote:
    """Public market snapshot of one contract, prices in the exchange's own price units."""

    bid: Decimal | None = None
    ask: Decimal | None = None
    mark: Decimal | None = None
    index: Decimal | None = None
    volume24h_usd: Decimal | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.mark if self.mark and self.mark > 0 else None


@dataclass(frozen=True, slots=True)
class PairAssessment:
    token: str
    a: Instrument
    b: Instrument
    common_step_tokens: Decimal
    min_qty_tokens: Decimal
    price_a_per_token: Decimal | None
    price_b_per_token: Decimal | None
    price_gap_pct: Decimal | None
    index_gap_pct: Decimal | None
    volume24h_weak_usd: Decimal | None
    suspicious_reason: str | None


def per_token(price: Decimal | None, instrument: Instrument) -> Decimal | None:
    if price is None or price <= 0:
        return None
    return price / instrument.price_unit_tokens


def match_instruments(left: list[Instrument], right: list[Instrument]) -> list[tuple[Instrument, Instrument]]:
    """Every combination of contracts with the same canonical token, ordered by token then symbols."""
    by_token: dict[str, list[Instrument]] = {}
    for instrument in right:
        by_token.setdefault(instrument.token, []).append(instrument)
    pairs = [(a, b) for a in left for b in by_token.get(a.token, [])]
    return sorted(pairs, key=lambda pair: (pair[0].token, pair[0].symbol_raw, pair[1].symbol_raw))


def assess_pair(
    a: Instrument,
    b: Instrument,
    quote_a: Quote | None,
    quote_b: Quote | None,
    max_price_gap_pct: Decimal = MAX_PRICE_GAP_PCT,
    max_index_gap_pct: Decimal = MAX_INDEX_GAP_PCT,
) -> PairAssessment:
    price_a = per_token(quote_a.mid, a) if quote_a else None
    price_b = per_token(quote_b.mid, b) if quote_b else None
    index_a = per_token(quote_a.index, a) if quote_a else None
    index_b = per_token(quote_b.index, b) if quote_b else None
    price_gap = price_gap_pct(price_a, price_b) if price_a and price_b else None
    index_gap = price_gap_pct(index_a, index_b) if index_a and index_b else None

    if price_gap is None:
        reason: str | None = "quote_missing"
    elif price_gap > max_price_gap_pct:
        # A wrong multiplier or a different token under the same ticker; no real gap is this wide.
        reason = "price_mismatch"
    else:
        reason = same_asset_verdict(index_a, index_b, max_index_gap_pct)

    volumes = [quote.volume24h_usd for quote in (quote_a, quote_b) if quote is not None]
    weak_volume = min(volumes) if len(volumes) == 2 and all(volume is not None for volume in volumes) else None
    return PairAssessment(
        token=a.token,
        a=a,
        b=b,
        common_step_tokens=common_step_tokens(a, b),
        min_qty_tokens=max(a.min_qty_tokens, b.min_qty_tokens),
        price_a_per_token=price_a,
        price_b_per_token=price_b,
        price_gap_pct=price_gap,
        index_gap_pct=index_gap,
        volume24h_weak_usd=weak_volume,
        suspicious_reason=reason,
    )
