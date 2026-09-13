"""Response shapes follow the Binance USDⓈ-M futures and wallet API documentation (docs/EXCHANGES.md)."""

from decimal import Decimal

from app.core.account import KeyPermissions
from app.exchanges.binance.adapter import parse_fee_rates, parse_one_way, parse_permissions, parse_usdt_balance

API_RESTRICTIONS = {
    "ipRestrict": False,
    "createTime": 1698645219000,
    "enableReading": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "enableMargin": False,
    "enableFutures": True,
    "permitsUniversalTransfer": False,
    "enableVanillaOptions": False,
    "enableFixApiTrade": False,
    "enableFixReadOnly": False,
    "enableSpotAndMarginTrading": False,
    "enablePortfolioMarginTrading": False,
}

BALANCE_V3 = [
    {
        "accountAlias": "SgsR",
        "asset": "BNB",
        "balance": "0.00000000",
        "crossWalletBalance": "0.00000000",
        "crossUnPnl": "0.00000000",
        "availableBalance": "0.00000000",
        "maxWithdrawAmount": "0.00000000",
        "marginAvailable": True,
        "updateTime": 0,
    },
    {
        "accountAlias": "SgsR",
        "asset": "USDT",
        "balance": "122.60735137",
        "crossWalletBalance": "122.60735137",
        "crossUnPnl": "0.00000000",
        "availableBalance": "102.33354500",
        "maxWithdrawAmount": "102.33354500",
        "marginAvailable": True,
        "updateTime": 1617939110373,
    },
]


def test_permissions_map_every_flag_the_terminal_needs():
    assert parse_permissions(API_RESTRICTIONS) == KeyPermissions(
        reading=True, futures=True, withdrawals=False, ip_restricted=False
    )


def test_missing_flags_stay_unknown():
    assert parse_permissions({"enableReading": True}) == KeyPermissions(reading=True)


def test_dual_side_position_true_means_hedge():
    assert parse_one_way({"dualSidePosition": True}) is False
    assert parse_one_way({"dualSidePosition": False}) is True
    assert parse_one_way({}) is None


def test_usdt_balance_is_exact_decimal():
    assert parse_usdt_balance(BALANCE_V3) == (Decimal("122.60735137"), Decimal("102.33354500"))
    assert parse_usdt_balance(BALANCE_V3[:1]) == (Decimal(0), Decimal(0))


def test_commission_fractions_become_percent():
    raw = {"symbol": "BTCUSDT", "makerCommissionRate": "0.000200", "takerCommissionRate": "0.000500"}
    assert parse_fee_rates(raw) == (Decimal("0.05"), Decimal("0.02"))
