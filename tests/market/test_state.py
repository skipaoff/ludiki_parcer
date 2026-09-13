from decimal import Decimal

from app.market.state import MarketState
from tests.core.helpers import instrument


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_binance_1000x_book_becomes_per_token():
    clock = Clock()
    state = MarketState(clock)
    state.set_instruments([instrument("binance", "1000PEPEUSDT", "PEPE", qty_unit_tokens="1000", price_unit_tokens="1000")])

    state.set_book("binance", "1000PEPEUSDT", [["0.0034082", "254020"], ["0.0034081", "0"]], [["0.0034083", "5451"]], 123)

    book = state.books[("binance", "1000PEPEUSDT")]
    assert book.bids[0].price == Decimal("0.0000034082")
    assert book.bids[0].qty_tokens == Decimal("254020000")
    assert len(book.bids) == 1  # zero-quantity levels are dropped
    assert book.asks[0].qty_tokens == Decimal("5451000")


def test_mexc_contract_levels_use_volume_in_contracts():
    state = MarketState(Clock())
    state.set_instruments([instrument("mexc", "PEPE_USDT", "PEPE", qty_unit_tokens="10000000", min_notional_usd="0")])

    # MEXC depth level: [price, volume in contracts, order count]
    state.set_book("mexc", "PEPE_USDT", [[3.4102e-06, 20, 1]], [[3.4121e-06, 26468, 2], [3.4103e-06, 45, 1]], 5)

    book = state.books[("mexc", "PEPE_USDT")]
    assert book.asks[0].price == Decimal("0.0000034103")  # re-sorted best first
    assert book.asks[0].qty_tokens == Decimal("450000000")
    assert book.bids[0].qty_tokens == Decimal("200000000")


def test_ages_and_unknown_symbols():
    clock = Clock()
    state = MarketState(clock)
    state.set_instruments([instrument("binance", "BTCUSDT", "BTC")])
    state.set_top("binance", "BTCUSDT", 100.0, 100.5, 1)
    state.set_top("binance", "NOTLISTED", 1.0, 2.0, 1)

    clock.now += 750
    assert state.top_age_ms(("binance", "BTCUSDT")) == 750
    assert ("binance", "NOTLISTED") not in state.tops
    assert state.book_age_ms(("binance", "BTCUSDT")) is None
