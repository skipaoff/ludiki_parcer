"""
VFP: Which contracts on any two exchanges form a tradable pair, and whether the pair is trustworthy — from instruments and public quotes.
Changes when: the rules for matching contracts or flagging a pair as suspicious change (PLAN.md, section 6).
Anti-goal:
1. Comparing prices in exchange units — 1000PEPEUSDT and PEPE_USDT only compare per token.
2. A missing quote or index passing as "same asset" — missing data is a reason, not a pass.
3. Reading exchanges or storage — instruments and quotes arrive as arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    volume24h_a_usd: Decimal | None = None
    volume24h_b_usd: Decimal | None = None

    @property
    def manual_only(self) -> bool:
        """A leg the exchange refuses orders on through the API. The gap is real; the button can never take it."""
        return not (self.a.api_tradable and self.b.api_tradable)


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


def rename_lonely_contracts(
    by_exchange: dict[str, list[Instrument]],
    index_per_token: dict[tuple[str, str], Decimal],
    max_index_diff_pct: Decimal = Decimal("0.25"),
) -> dict[str, list[Instrument]]:
    """
    Give a contract that nobody else names the same way the name the rest of the market uses for it.

    A venue writes RAYSOL where five others write RAY, TRUMPSOL where eight write TRUMP, NOKIA for NOK. The
    name alone proves nothing, so the index price decides, and a quarter of a percent is the whole allowance:
    every one of the renames measured on 23.09.2026 agreed to 0.22 % or better, while gate's GIGGLEMAX sat
    0.49 % from aster's MAX with perpetual prices 5 % apart — two different coins whose indices happened to
    cross. Only contracts with no pair at all are renamed, and only onto a token that exists on another
    exchange, so nothing that already trades can lose its name this way.
    """
    venues_of: dict[str, set[str]] = {}
    for exchange, instruments in by_exchange.items():
        for item in instruments:
            venues_of.setdefault(item.token, set()).add(exchange)

    renamed: dict[str, list[Instrument]] = {}
    for exchange, instruments in by_exchange.items():
        fixed = []
        for item in instruments:
            token = item.token if len(venues_of.get(item.token, ())) > 1 else _name_of_the_market(item, exchange, venues_of, index_per_token, max_index_diff_pct)
            fixed.append(item if token == item.token else replace(item, token=token))
        renamed[exchange] = fixed
    return renamed


def _name_of_the_market(
    item: Instrument,
    exchange: str,
    venues_of: dict[str, set[str]],
    index_per_token: dict[tuple[str, str], Decimal],
    max_index_diff_pct: Decimal,
) -> str:
    """The established token this lonely contract is priced like, or its own name when there is none."""
    mine = index_per_token.get((exchange, item.token))
    if mine is None or mine <= 0:
        return item.token
    best, best_venues = item.token, 0
    for token, venues in venues_of.items():
        # The other name has to be an established one: on another exchange, and long enough not to match by chance.
        if len(token) < 3 or len(venues) <= best_venues or venues == {exchange}:
            continue
        if not (item.token.startswith(token) or item.token.endswith(token)) or token == item.token:
            continue
        for other in venues - {exchange}:
            theirs = index_per_token.get((other, token))
            if theirs is None or theirs <= 0:
                continue
            if price_gap_pct(mine, theirs) <= max_index_diff_pct:
                best, best_venues = token, len(venues)
                break
    return best


def match_all(by_exchange: dict[str, list[Instrument]], order: list[str]) -> list[tuple[Instrument, Instrument]]:
    """Pairs across every two exchanges; the exchange earlier in order is always leg a, so pair keys stay stable."""
    present = [name for name in order if name in by_exchange]
    pairs = []
    for index, left in enumerate(present):
        for right in present[index + 1 :]:
            pairs.extend(match_instruments(by_exchange[left], by_exchange[right]))
    return pairs


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
    # A venue that publishes no index (Variational) is compared by its mark price against the other side's index.
    # Both indices missing stays "index_missing": two marks prove nothing about the underlying asset.
    if index_a is None and index_b is not None and quote_a is not None:
        index_a = per_token(quote_a.mark, a)
    elif index_b is None and index_a is not None and quote_b is not None:
        index_b = per_token(quote_b.mark, b)
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
        volume24h_a_usd=quote_a.volume24h_usd if quote_a else None,
        volume24h_b_usd=quote_b.volume24h_usd if quote_b else None,
    )
