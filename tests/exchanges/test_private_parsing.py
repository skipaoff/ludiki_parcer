"""Private answers shaped after the Binance and MEXC documentation; confirm against real keys (docs/EXCHANGES.md)."""

from decimal import Decimal

from app.core.schemas import LegSide
from app.exchanges.binance import adapter as binance
from app.exchanges.mexc import adapter as mexc
from tests.core.helpers import instrument

BINANCE_INSTRUMENTS = {
    "1000PEPEUSDT": instrument("binance", "1000PEPEUSDT", "PEPE", qty_unit_tokens="1000", price_unit_tokens="1000"),
    "SOLUSDT": instrument("binance", "SOLUSDT", "SOL", qty_step_units="0.01"),
}
MEXC_INSTRUMENTS = {
    "1000BONK_USDT": instrument("mexc", "1000BONK_USDT", "BONK", qty_unit_tokens="10000000", price_unit_tokens="1000"),
    "SOL_USDT": instrument("mexc", "SOL_USDT", "SOL", qty_unit_tokens="0.1"),
}

BINANCE_POSITION_RISK_V2 = [
    {
        "entryPrice": "0.0034100", "marginType": "isolated", "isAutoAddMargin": "false", "isolatedMargin": "113.60",
        "leverage": "3", "liquidationPrice": "0.0045300", "markPrice": "0.0034082", "maxNotionalValue": "25000",
        "positionAmt": "-100000", "notional": "-340.82", "isolatedWallet": "113.66", "symbol": "1000PEPEUSDT",
        "unRealizedProfit": "0.18", "positionSide": "BOTH", "updateTime": 1789327919095,
    },
    {"symbol": "SOLUSDT", "positionAmt": "0.00", "entryPrice": "0.0", "markPrice": "143.0", "liquidationPrice": "0", "positionSide": "BOTH"},
    {"symbol": "NOTINCATALOG", "positionAmt": "5", "entryPrice": "1", "positionSide": "BOTH"},
]


def test_binance_short_multiplier_position_per_token():
    [position] = binance.parse_positions(BINANCE_POSITION_RISK_V2, BINANCE_INSTRUMENTS)
    assert position.side is LegSide.SHORT
    assert position.qty_tokens == Decimal("100000000")
    assert position.entry_price == Decimal("0.0000034100")
    assert position.liquidation_price == Decimal("0.0000045300")
    assert (position.leverage, position.margin_mode, position.token) == (3, "isolated", "PEPE")


def test_binance_account_and_funding():
    account = {"totalMarginBalance": "1003.12", "availableBalance": "850.10", "totalInitialMargin": "150.00", "assets": []}
    balance = binance.parse_balance(account)
    assert (balance.equity_usd, balance.available_usd, balance.margin_used_usd) == (Decimal("1003.12"), Decimal("850.10"), Decimal("150.00"))

    income = [
        {"symbol": "SOLUSDT", "incomeType": "FUNDING_FEE", "income": "-0.0150", "asset": "USDT", "time": 2000},
        {"symbol": "SOLUSDT", "incomeType": "FUNDING_FEE", "income": "0.0040", "asset": "USDT", "time": 3000},
        {"symbol": "SOLUSDT", "incomeType": "FUNDING_FEE", "income": "1.0000", "asset": "USDT", "time": 500},
        {"symbol": "SOLUSDT", "incomeType": "COMMISSION", "income": "-0.2", "asset": "USDT", "time": 2500},
    ]
    assert binance.parse_funding(income, since_ms=1000) == Decimal("-0.0110")


MEXC_OPEN_POSITIONS = {
    "success": True,
    "code": 0,
    "data": [
        {
            "positionId": 1394650, "symbol": "1000BONK_USDT", "positionType": 1, "openType": 2, "state": 1,
            "holdVol": 3, "frozenVol": 0, "closeVol": 0, "holdAvgPrice": 0.0027425, "openAvgPrice": 0.0027425,
            "closeAvgPrice": 0, "liquidatePrice": 0.0019, "oim": 27.4, "im": 27.4, "holdFee": 0, "realised": -0.01,
            "leverage": 5, "createTime": 1789327000000, "updateTime": 1789327919000, "autoAddIm": False,
        },
        {"positionId": 2, "symbol": "SOL_USDT", "positionType": 2, "openType": 1, "holdVol": 0, "holdAvgPrice": 143},
    ],
}


def test_mexc_long_contract_position_per_token():
    [position] = mexc.parse_positions(MEXC_OPEN_POSITIONS, MEXC_INSTRUMENTS)
    assert position.side is LegSide.LONG
    assert position.qty_tokens == Decimal("30000000")  # 3 contracts × 10,000 units × 1000 BONK
    assert position.entry_price == Decimal("0.0000027425")
    assert position.liquidation_price == Decimal("0.0000019")
    assert position.mark_price is None
    assert (position.margin_mode, position.leverage) == ("cross", 5)


def test_mexc_assets_and_funding_pages():
    assets = {
        "success": True,
        "code": 0,
        "data": [{"currency": "USDT", "equity": 510.5, "availableBalance": 400.25, "positionMargin": 100, "frozenBalance": 10.25}],
    }
    balance = mexc.parse_balance(assets)
    assert (balance.equity_usd, balance.available_usd, balance.margin_used_usd) == (Decimal("510.5"), Decimal("400.25"), Decimal("110.25"))

    page = {
        "success": True,
        "code": 0,
        "data": {
            "pageSize": 2, "totalCount": 5, "totalPage": 3, "currentPage": 1,
            "resultList": [
                {"symbol": "SOL_USDT", "positionType": 2, "funding": 0.012, "rate": 0.0001, "settleTime": 5000},
                {"symbol": "SOL_USDT", "positionType": 2, "funding": -0.002, "rate": -0.00002, "settleTime": 4000},
            ],
        },
    }
    assert mexc.parse_funding(page, since_ms=3000) == (Decimal("0.010"), True)
    assert mexc.parse_funding(page, since_ms=4500) == (Decimal("0.012"), False)
