"""Response shapes follow the MEXC contract API documentation (docs/EXCHANGES.md); confirm on the first real check."""

from decimal import Decimal

import pytest

from app.exchanges.mexc.adapter import MexcResponseError, parse_fee_rates, parse_one_way, parse_usdt_assets

ASSETS = {
    "success": True,
    "code": 0,
    "data": [
        {
            "currency": "USDT",
            "positionMargin": 0,
            "availableBalance": 95.5,
            "cashBalance": 100.25,
            "frozenBalance": 0,
            "equity": 100.25,
            "unrealized": 0,
            "bonus": 0,
        },
        {"currency": "BTC", "availableBalance": 0, "cashBalance": 0, "equity": 0},
    ],
}


def test_position_mode_codes():
    assert parse_one_way({"success": True, "code": 0, "data": 2}) is True
    assert parse_one_way({"success": True, "code": 0, "data": 1}) is False
    assert parse_one_way({"success": True, "code": 0, "data": 7}) is None


def test_usdt_assets():
    assert parse_usdt_assets(ASSETS) == (Decimal("100.25"), Decimal("95.5"))
    assert parse_usdt_assets({"success": True, "code": 0, "data": []}) == (Decimal(0), Decimal(0))


def test_unsuccessful_answer_raises_with_the_exchange_code():
    with pytest.raises(MexcResponseError, match="code 401"):
        parse_usdt_assets({"success": False, "code": 401, "message": "Not logged in"})


@pytest.mark.parametrize(
    "data",
    [
        {"takerFeeRate": 0.0005, "makerFeeRate": 0.0001},
        {"takerFee": 0.0005, "makerFee": 0.0001},
        [{"takerFeeRate": 0.0005, "makerFeeRate": 0.0001}],
    ],
)
def test_fee_rates_accept_known_spellings(data):
    assert parse_fee_rates({"success": True, "code": 0, "data": data}) == (Decimal("0.05"), Decimal("0.01"))


def test_fee_rates_unknown_shape_is_none():
    assert parse_fee_rates({"success": True, "code": 0, "data": {"level": 1}}) is None
