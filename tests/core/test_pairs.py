from decimal import Decimal

from app.core.pairs import Quote, assess_pair, match_instruments
from tests.core.helpers import D, instrument


def pepe_pair():
    # Binance 1000PEPEUSDT: price and quantity per 1000 PEPE. MEXC PEPE_USDT: contract of 10,000,000 PEPE, price per PEPE.
    binance = instrument("binance", "1000PEPEUSDT", "PEPE", qty_unit_tokens="1000", price_unit_tokens="1000")
    mexc = instrument("mexc", "PEPE_USDT", "PEPE", qty_unit_tokens="10000000", min_notional_usd="0")
    return binance, mexc


def test_match_pairs_every_contract_of_the_same_token():
    binance, mexc = pepe_pair()
    btc_binance = instrument("binance", "BTCUSDT", "BTC")
    only_mexc = instrument("mexc", "ONLYMEXC_USDT", "ONLYMEXC")

    pairs = match_instruments([btc_binance, binance], [mexc, only_mexc])

    assert [(a.symbol_raw, b.symbol_raw) for a, b in pairs] == [("1000PEPEUSDT", "PEPE_USDT")]


def test_multiplier_contracts_compare_per_token():
    binance, mexc = pepe_pair()
    assessment = assess_pair(
        binance,
        mexc,
        Quote(bid=D("0.0100"), ask=D("0.0102"), index=D("0.0101"), volume24h_usd=D("50000000")),
        Quote(bid=D("0.0000100"), ask=D("0.0000102"), index=D("0.0000101"), volume24h_usd=D("3000000")),
    )

    assert assessment.price_a_per_token == D("0.0000101")
    assert assessment.price_gap_pct == 0
    assert assessment.suspicious_reason is None
    assert assessment.common_step_tokens == D("10000000")
    assert assessment.volume24h_weak_usd == D("3000000")


def test_wrong_multiplier_shows_up_as_price_mismatch():
    binance, mexc = pepe_pair()
    assessment = assess_pair(binance, mexc, Quote(mark=D("0.0101")), Quote(mark=D("0.0101"), index=D("0.0101")))
    assert assessment.suspicious_reason == "price_mismatch"


def test_index_disagreement_flags_a_different_asset_even_when_prices_match():
    a = instrument("binance", "ONEUSDT", "ONE")
    b = instrument("mexc", "ONE_USDT", "ONE", min_notional_usd="0")
    assessment = assess_pair(a, b, Quote(mark=D("0.01"), index=D("0.0100")), Quote(mark=D("0.01"), index=D("0.0105")))
    assert assessment.suspicious_reason == "index_mismatch"
    assert assessment.index_gap_pct == Decimal("5")


def test_missing_data_is_a_reason_not_a_pass():
    a = instrument("binance", "XUSDT", "X")
    b = instrument("mexc", "X_USDT", "X", min_notional_usd="0")
    assert assess_pair(a, b, None, Quote(mark=D("1"))).suspicious_reason == "quote_missing"
    assert assess_pair(a, b, Quote(mark=D("1")), Quote(mark=D("1"))).suspicious_reason == "index_missing"
    assert assess_pair(a, b, Quote(mark=D("1")), None).volume24h_weak_usd is None


def test_mid_prefers_book_over_mark():
    assert Quote(bid=D("1"), ask=D("3"), mark=D("10")).mid == D("2")
    assert Quote(bid=D("0"), ask=D("3"), mark=D("10")).mid == D("10")
    assert Quote().mid is None
