"""Order answers shaped after the Binance and MEXC documentation; confirm with the trial trade (scripts/trial_trade.py)."""

from decimal import Decimal

import pytest

from app.core.legs import OrderOutcome
from app.exchanges.binance import adapter as binance
from app.exchanges.mexc import adapter as mexc
from tests.core.helpers import instrument

PEPE_BINANCE = instrument("binance", "1000PEPEUSDT", "PEPE", qty_unit_tokens="1000", price_unit_tokens="1000")
BONK_MEXC = instrument("mexc", "1000BONK_USDT", "BONK", qty_unit_tokens="10000000", price_unit_tokens="1000")


def binance_order(status, executed, avg="0.0034100"):
    return {
        "clientOrderId": "lkabcol0", "cumQty": "0", "cumQuote": "340.99", "executedQty": executed, "orderId": 22542179,
        "avgPrice": avg, "origQty": "100000", "price": "0", "reduceOnly": False, "side": "BUY", "positionSide": "BOTH",
        "status": status, "stopPrice": "0", "closePosition": False, "symbol": "1000PEPEUSDT", "timeInForce": "GTC",
        "type": "MARKET", "origType": "MARKET", "updateTime": 1789327919095, "workingType": "CONTRACT_PRICE",
        "priceProtect": False,
    }


@pytest.mark.parametrize(
    ("status", "executed", "outcome"),
    [
        ("FILLED", "100000", OrderOutcome.FILLED),
        ("EXPIRED", "60000", OrderOutcome.PARTIAL),
        ("EXPIRED", "0", OrderOutcome.REJECTED),
        ("NEW", "0", OrderOutcome.UNKNOWN),
        ("PARTIALLY_FILLED", "30000", OrderOutcome.UNKNOWN),
    ],
)
def test_binance_order_outcomes(status, executed, outcome):
    report = binance.parse_order(binance_order(status, executed), PEPE_BINANCE, "lkabcol0", Decimal("100000000"), 1, 2)
    assert report.outcome is outcome
    assert report.filled_tokens == Decimal(executed) * 1000


def test_binance_filled_order_per_token_and_fills():
    report = binance.parse_order(binance_order("FILLED", "100000"), PEPE_BINANCE, "lkabcol0", Decimal("100000000"), 1, 2)
    assert report.avg_price == Decimal("0.0000034100")
    assert report.exchange_order_id == "22542179"

    fills = binance.parse_fills(
        [{"buyer": True, "commission": "-0.17049500", "commissionAsset": "USDT", "id": 698759, "maker": False, "orderId": 22542179,
          "price": "0.0034100", "qty": "100000", "quoteQty": "340.99", "realizedPnl": "0", "side": "BUY", "positionSide": "BOTH",
          "symbol": "1000PEPEUSDT", "time": 1789327919096}],
        PEPE_BINANCE,
    )
    assert fills[0].qty_tokens == Decimal("100000000")
    assert fills[0].fee == Decimal("0.17049500") and fills[0].fee_asset == "USDT" and fills[0].is_maker is False


def mexc_order(state, deal_vol):
    return {
        "orderId": "102015012431820288", "symbol": "1000BONK_USDT", "positionId": 1394650, "price": 0, "vol": 3,
        "leverage": 5, "side": 1, "category": 1, "orderType": 5, "dealAvgPrice": 0.0027425, "dealVol": deal_vol,
        "orderMargin": 5.49, "takerFee": 0.0411, "makerFee": 0, "profit": 0, "feeCurrency": "USDT", "openType": 2,
        "state": state, "externalOid": "lkabcol0", "errorCode": 0, "usedMargin": 5.49, "createTime": 1789327919000,
        "updateTime": 1789327919100,
    }


@pytest.mark.parametrize(
    ("state", "deal_vol", "outcome"),
    [(3, 3, OrderOutcome.FILLED), (3, 2, OrderOutcome.PARTIAL), (4, 1, OrderOutcome.PARTIAL), (5, 0, OrderOutcome.REJECTED), (2, 0, OrderOutcome.UNKNOWN)],
)
def test_mexc_order_states(state, deal_vol, outcome):
    report = mexc.parse_order(mexc_order(state, deal_vol), BONK_MEXC, "lkabcol0", Decimal("30000000"), 1, 2)
    assert report.outcome is outcome
    assert report.filled_tokens == Decimal(deal_vol) * Decimal("10000000")


def test_mexc_order_price_fee_and_fills():
    report = mexc.parse_order(mexc_order(3, 3), BONK_MEXC, "lkabcol0", Decimal("30000000"), 1, 2)
    assert report.avg_price == Decimal("0.0000027425")
    assert report.fee_usd == Decimal("0.0411")

    fills = mexc.parse_fills(
        {"success": True, "code": 0, "data": [{"id": "5991", "symbol": "1000BONK_USDT", "side": 1, "vol": 3, "price": 0.0027425,
          "feeCurrency": "USDT", "fee": 0.0411, "timestamp": 1789327919050, "profit": 0, "category": 1, "orderId": "102015012431820288",
          "positionMode": 2, "isTaker": True}]},
        BONK_MEXC,
    )
    assert fills[0].qty_tokens == Decimal("30000000") and fills[0].is_maker is False


def test_mexc_submit_answer_shapes():
    assert mexc.submitted_order_id({"success": True, "code": 0, "data": 102015012431820288}) == "102015012431820288"
    assert mexc.submitted_order_id({"success": True, "code": 0, "data": {"orderId": "7", "ts": 1}}) == "7"
    assert mexc.vol_value(Decimal("3")) == 3 and mexc.vol_value(Decimal("0.5")) == 0.5
