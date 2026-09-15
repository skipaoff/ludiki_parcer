from decimal import Decimal

from app.core.funding import FundingRate, next_settlement_ms, pair_funding, settlements_within
from app.core.interest import interest

HOUR = 3_600_000
NOW = 1_789_480_000_000


def rate(pct, hours, next_in_ms):
    return FundingRate(Decimal(pct), Decimal(hours), None if next_in_ms is None else NOW + next_in_ms)


def test_a_stale_next_settlement_rolls_forward_by_whole_intervals():
    assert next_settlement_ms(rate("0.01", 8, -HOUR), NOW) == NOW + 7 * HOUR
    assert next_settlement_ms(rate("0.01", 8, 0), NOW) == NOW + 8 * HOUR
    assert next_settlement_ms(rate("0.01", 8, 10 * 60_000), NOW) == NOW + 10 * 60_000


def test_payments_are_counted_at_settlement_times_not_averaged():
    soon = rate("0.01", 8, 10 * 60_000)
    assert settlements_within(soon, NOW, 8 * HOUR) == 1
    assert settlements_within(soon, NOW, 8 * HOUR + 10 * 60_000) == 2
    assert settlements_within(rate("0.01", 8, 9 * HOUR), NOW, 8 * HOUR) == 0
    # Without a schedule the rate accrues in proportion to time.
    assert settlements_within(rate("0.01", 8, None), NOW, 4 * HOUR) == Decimal("0.5")


def test_pair_funding_long_pays_positive_rate_short_receives_it():
    long = rate("0.01", 8, 10 * 60_000)  # pays once within 8 h, in 10 minutes
    short = rate("0.03", 4, 2 * HOUR)  # receives at 2 h and 6 h
    funding = pair_funding(long, short, NOW, Decimal(8))
    assert funding.horizon_pct == Decimal("-0.01") + Decimal("0.06")
    assert funding.hourly_pct == Decimal("0.03") / 4 - Decimal("0.01") / 8
    assert (funding.next_ms, funding.next_pct) == (NOW + 10 * 60_000, Decimal("-0.01"))


def test_settlements_at_the_same_moment_add_up_and_unknown_legs_make_it_unknown():
    long, short = rate("-0.02", 8, HOUR), rate("0.05", 8, HOUR)
    funding = pair_funding(long, short, NOW, Decimal(1))
    assert funding.next_pct == Decimal("0.07") and funding.horizon_pct == Decimal("0.07")
    assert pair_funding(long, None, NOW, Decimal(8)) is None


def test_interest_parts_and_limits():
    full = interest(Decimal("2.5"), Decimal("5000"), Decimal("1000"), 600_000, Decimal("20000000"), False)
    assert (full.score, full.result, full.depth, full.stability, full.liquidity) == (100, 50, 20, 15, 15)
    half = interest(Decimal("1"), Decimal("1500"), Decimal("1000"), 150_000, Decimal("1000000"), False)
    assert (half.result, half.depth, half.stability, half.liquidity) == (25, 10, 8, 8)
    assert interest(Decimal("-0.1"), Decimal("5000"), Decimal("1000"), 600_000, Decimal("2e7"), False).score == 0
    assert interest(Decimal("3"), Decimal("5000"), Decimal("1000"), 600_000, Decimal("2e7"), True).score == 0
    assert interest(None, None, Decimal("1000"), None, None, False) is None
