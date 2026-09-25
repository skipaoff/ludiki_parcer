from decimal import Decimal

from app.core.funding import PairFunding
from app.core.measure import measure

SIZE = Decimal("1000")


def funding(horizon_pct: str) -> PairFunding:
    return PairFunding(hourly_pct=Decimal("0.01"), horizon_pct=Decimal(horizon_pct), next_ms=None, next_pct=None)


def test_expected_result_is_book_profit_plus_funding_over_the_horizon():
    measured = measure(Decimal("1.20"), funding("-0.40"), SIZE * 3, SIZE, 300_000, Decimal("5000000"), False)

    assert measured.total_pct == Decimal("0.80")
    assert measured.interest == measured.score.score > 0


def test_unknown_funding_leaves_the_profit_alone_instead_of_counting_it_as_zero_cost():
    known = measure(Decimal("1.20"), funding("0"), SIZE, SIZE, 0, None, False)
    unknown = measure(Decimal("1.20"), None, SIZE, SIZE, 0, None, False)

    assert known.total_pct == unknown.total_pct == Decimal("1.20")
    assert unknown.funding is None


def test_a_gap_without_a_book_measurement_has_no_result_and_no_score():
    measured = measure(None, funding("0.10"), None, SIZE, 1_000, Decimal("1000000"), False)

    assert measured.total_pct is None and measured.interest is None


def test_a_row_the_terminal_refuses_to_trust_keeps_numbers_but_gets_no_score():
    measured = measure(Decimal("2.00"), funding("0"), SIZE * 3, SIZE, 300_000, Decimal("5000000"), False, scored=False)

    assert measured.total_pct == Decimal("2.00")
    assert measured.score is None and measured.interest is None


def test_a_suspicious_pair_scores_zero_however_good_the_numbers_look():
    measured = measure(Decimal("9.00"), funding("0"), SIZE * 10, SIZE, 600_000, Decimal("10000000"), True)

    assert measured.total_pct == Decimal("9.00")
    assert measured.interest == 0
