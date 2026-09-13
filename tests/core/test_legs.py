from app.core.actions import (
    AlertLevel,
    CloseLeg,
    MarkLegLost,
    MarkTradeClosed,
    MarkTradeFailed,
    MarkTradeOpen,
    QueryOrderStatus,
    RaiseAlert,
    ReduceLeg,
)
from app.core.legs import CloseResult, LegResult, OrderOutcome, decide_close, decide_open
from app.core.schemas import LegSide
from tests.core.helpers import D

LONG, SHORT = LegSide.LONG, LegSide.SHORT
FILLED, PARTIAL, REJECTED, UNKNOWN = OrderOutcome.FILLED, OrderOutcome.PARTIAL, OrderOutcome.REJECTED, OrderOutcome.UNKNOWN


def open_decision(long, short, requested="100", step="10", min_tokens="10"):
    return decide_open(long, short, D(requested), D(step), D(min_tokens))


def test_both_legs_filled_opens_the_trade():
    assert open_decision(LegResult(LONG, FILLED, D("100")), LegResult(SHORT, FILLED, D("100"))) == [MarkTradeOpen(D("100"))]


def test_one_leg_rejected_closes_the_filled_hedge():
    actions = open_decision(LegResult(LONG, FILLED, D("100")), LegResult(SHORT, REJECTED, D("0")))
    assert actions == [
        CloseLeg(LONG, D("100")),
        MarkTradeFailed("leg_rejected"),
        RaiseAlert(AlertLevel.CRITICAL, "leg_rejected_hedge_closed"),
    ]


def test_both_rejected_needs_no_orders():
    actions = open_decision(LegResult(LONG, REJECTED, D("0")), LegResult(SHORT, REJECTED, D("0")))
    assert not any(isinstance(action, (CloseLeg, ReduceLeg)) for action in actions)
    assert MarkTradeFailed("both_rejected") in actions


def test_partial_fill_is_equalized_down_to_the_common_step():
    actions = open_decision(LegResult(LONG, FILLED, D("100")), LegResult(SHORT, PARTIAL, D("67")))
    assert actions == [
        ReduceLeg(LONG, D("40")),
        ReduceLeg(SHORT, D("7")),
        MarkTradeOpen(D("60")),
        RaiseAlert(AlertLevel.WARNING, "partial_fill_equalized"),
    ]


def test_partial_fill_below_minimum_closes_both_legs():
    actions = open_decision(LegResult(LONG, FILLED, D("100")), LegResult(SHORT, PARTIAL, D("8")))
    assert actions[:3] == [CloseLeg(LONG, D("100")), CloseLeg(SHORT, D("8")), MarkTradeFailed("partial_below_min")]


def test_unknown_status_only_queries_and_alerts():
    actions = open_decision(LegResult(LONG, FILLED, D("100")), LegResult(SHORT, UNKNOWN, D("0")))
    assert actions == [QueryOrderStatus(SHORT), RaiseAlert(AlertLevel.CRITICAL, "leg_status_unknown")]


def test_no_open_decision_leaves_an_unhedged_leg_silently():
    outcomes = [(FILLED, "100"), (PARTIAL, "55"), (PARTIAL, "3"), (REJECTED, "0")]
    for long_outcome, long_qty in outcomes:
        for short_outcome, short_qty in outcomes:
            actions = open_decision(LegResult(LONG, long_outcome, D(long_qty)), LegResult(SHORT, short_outcome, D(short_qty)))
            opened = [a for a in actions if isinstance(a, MarkTradeOpen)]
            if opened:
                target = opened[0].qty_tokens
                long_left = D(long_qty) - sum((a.qty_tokens for a in actions if isinstance(a, ReduceLeg) and a.side is LONG), D(0))
                short_left = D(short_qty) - sum((a.qty_tokens for a in actions if isinstance(a, ReduceLeg) and a.side is SHORT), D(0))
                assert long_left == short_left == target
            else:
                for side, qty in ((LONG, long_qty), (SHORT, short_qty)):
                    if D(qty) > 0:
                        assert CloseLeg(side, D(qty)) in actions


def test_close_retries_then_marks_leg_lost():
    long_done = CloseResult(LONG, FILLED, D("0"))
    short_stuck = CloseResult(SHORT, REJECTED, D("100"))

    assert decide_close(long_done, CloseResult(SHORT, FILLED, D("0")), attempt=1, max_attempts=3) == [MarkTradeClosed()]
    assert decide_close(long_done, short_stuck, attempt=1, max_attempts=3) == [CloseLeg(SHORT, D("100"))]
    assert decide_close(long_done, short_stuck, attempt=3, max_attempts=3) == [
        MarkLegLost(SHORT),
        RaiseAlert(AlertLevel.CRITICAL, "leg_close_failed"),
    ]
