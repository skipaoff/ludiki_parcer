from decimal import Decimal

import pytest

from app.core.qty import QtyPlan, QtyRejected, decimal_lcm, floor_to_step, plan_quantity, to_units
from tests.core.helpers import D, instrument


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [("0.001", "0.0001", "0.001"), ("0.5", "0.3", "1.5"), ("1000", "100000", "100000"), ("1", "1", "1"), ("10", "0.1", "10")],
)
def test_decimal_lcm(a, b, expected):
    assert decimal_lcm(D(a), D(b)) == D(expected)


def test_floor_to_step_never_rounds_up():
    assert floor_to_step(D("12.3456"), D("0.01")) == D("12.34")
    assert floor_to_step(D("0.009"), D("0.01")) == 0


def test_same_token_count_on_binance_multiplier_and_mexc_contracts():
    # Binance 1000PEPEUSDT: one lot = 1000 PEPE, step 1 lot, price quoted per 1000 PEPE.
    binance = instrument("binance", "1000PEPEUSDT", "PEPE", qty_unit_tokens="1000", price_unit_tokens="1000")
    # MEXC PEPE_USDT: one contract = 100000 PEPE.
    mexc = instrument("mexc", "PEPE_USDT", "PEPE", qty_unit_tokens="100000", min_notional_usd="0")

    plan = plan_quantity(D("1000"), D("0.00001"), long=mexc, short=binance)

    assert plan == QtyPlan(qty_tokens=D("100000000"), units_long=D("1000"), units_short=D("100000"))
    assert plan.units_long * mexc.qty_unit_tokens == plan.units_short * binance.qty_unit_tokens


def test_quantity_is_floored_to_the_coarser_exchange_step():
    binance = instrument("binance", "SOLUSDT", "SOL", qty_step_units="0.01", min_qty_units="0.01")
    mexc = instrument("mexc", "SOL_USDT", "SOL", qty_unit_tokens="0.1", min_qty_units="1")

    plan = plan_quantity(D("1000"), D("143.05"), long=mexc, short=binance)

    assert isinstance(plan, QtyPlan)
    assert plan.qty_tokens == D("6.9")
    assert plan.units_long == D("69")
    assert plan.units_short == D("6.9")


@pytest.mark.parametrize(
    ("size", "price", "overrides", "reason"),
    [
        ("5", "100", {}, "size_below_common_step"),
        ("150", "100", {"min_notional_usd": "200"}, "below_min_notional:binance"),
        ("1000", "1", {"max_market_qty_units": "500"}, "above_max_market_qty:binance"),
        ("150", "100", {"min_qty_units": "2"}, "below_min_qty:binance"),
        ("0", "100", {}, "non_positive_input"),
    ],
)
def test_plan_quantity_rejections(size, price, overrides, reason):
    binance = instrument("binance", "XUSDT", "X", **overrides)
    mexc = instrument("mexc", "X_USDT", "X", min_notional_usd="0")
    assert plan_quantity(D(size), D(price), long=binance, short=mexc) == QtyRejected(reason)


def test_to_units_refuses_amount_off_the_exchange_step():
    mexc = instrument("mexc", "X_USDT", "X", qty_unit_tokens="10")
    with pytest.raises(ValueError):
        to_units(Decimal("15"), mexc)
