"""Frames recorded from Binance and MEXC websockets on 13.09.2026 (trimmed)."""

from decimal import Decimal

import orjson

from app.market import binance_streams, mexc_market
from app.market.state import MarketState
from tests.core.helpers import instrument

BINANCE_BOOK_TICKER = (
    '{"stream":"1000pepeusdt@bookTicker","data":{"e":"bookTicker","u":11547818761677,"s":"1000PEPEUSDT",'
    '"ps":"1000PEPEUSDT","b":"0.0034082","B":"254020","a":"0.0034083","A":"5451","T":1789327919095,"E":1789327919095,"st":1}}'
)
BINANCE_DEPTH = (
    '{"stream":"ethusdt@depth20@100ms","data":{"e":"depthUpdate","E":1789327919113,"T":1789327919111,"s":"ETHUSDT",'
    '"ps":"ETHUSDT","U":1,"u":2,"pu":0,"b":[["2505.11","145.815"],["2505.10","0.029"]],"a":[["2505.12","3.5"],["2505.13","0.8"]]}}'
)
MEXC_DEPTH_FULL = {
    "symbol": "PEPE_USDT",
    "data": {
        "cts": 1789327957142,
        "asks": [[3.4103e-06, 45, 1], [3.4104e-06, 15, 1]],
        "bids": [[3.4102e-06, 20, 1], [3.4101e-06, 20, 1]],
    },
    "channel": "push.depth.full",
    "ts": 1789327957150,
}


def state_with(*items) -> MarketState:
    state = MarketState(lambda: 2_000_000_000_000.0)
    state.set_instruments(list(items))
    return state


def test_binance_book_ticker_updates_per_token_top():
    state = state_with(instrument("binance", "1000PEPEUSDT", "PEPE", qty_unit_tokens="1000", price_unit_tokens="1000"))
    binance_streams.handle_frame(state, BINANCE_BOOK_TICKER)
    top = state.tops[("binance", "1000PEPEUSDT")]
    assert abs(top.bid - 0.0000034082) < 1e-15
    assert top.exchange_ts_ms == 1789327919095
    assert state.messages["binance"] == 1


def test_binance_partial_depth_becomes_a_book():
    state = state_with(instrument("binance", "ETHUSDT", "ETH"))
    binance_streams.handle_frame(state, BINANCE_DEPTH)
    book = state.books[("binance", "ETHUSDT")]
    assert book.bids[0].price == Decimal("2505.11") and book.asks[0].qty_tokens == Decimal("3.5")
    assert book.exchange_ts_ms == 1789327919111


def test_binance_control_answer_is_ignored():
    state = state_with()
    binance_streams.handle_frame(state, '{"result":null,"id":1}')
    assert state.messages == {}


def test_mexc_depth_full_levels_are_price_volume_orders():
    state = state_with(instrument("mexc", "PEPE_USDT", "PEPE", qty_unit_tokens="10000000", min_notional_usd="0"))
    mexc_market.handle_frame(state, orjson.dumps(MEXC_DEPTH_FULL))
    book = state.books[("mexc", "PEPE_USDT")]
    assert book.asks[0].qty_tokens == Decimal("450000000")
    assert book.bids[0].price == Decimal("0.0000034102")


def test_mexc_ticker_poll_keeps_only_contracts_with_both_prices():
    payload = {
        "success": True,
        "code": 0,
        "data": [
            {"symbol": "BTC_USDT", "bid1": 77222, "ask1": 77222.1, "timestamp": 5, "maxBidPrice": 84985.5},
            {"symbol": "DEAD_USDT", "bid1": 0, "ask1": 1.0, "timestamp": 5},
        ],
    }
    assert mexc_market.ticker_tops(payload) == [("BTC_USDT", 77222.0, 77222.1, 5)]


def test_mexc_subscription_messages():
    assert [orjson.loads(m) for m in mexc_market.depth_messages(["BTC_USDT"], True)] == [
        {"method": "sub.depth.full", "param": {"symbol": "BTC_USDT", "limit": 20}}
    ]
    assert orjson.loads(next(iter(binance_streams.control_messages(["btcusdt@bookTicker"], False))))["method"] == "UNSUBSCRIBE"
