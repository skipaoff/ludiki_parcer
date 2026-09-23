from decimal import Decimal

from app.core.pairs import Quote, assess_pair, match_instruments, rename_lonely_contracts
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


def market(**prices: str) -> dict[tuple[str, str], Decimal]:
    """Index price per (exchange, token), written as "exchange__token"."""
    return {tuple(key.split("__")): D(value) for key, value in prices.items()}


def test_a_contract_nobody_else_names_the_same_way_takes_the_market_name():
    # Binance writes RAYSOL, three other venues write RAY, and every one of them prices it the same.
    lonely = instrument("binance", "RAYSOLUSDT", "RAYSOL")
    catalog = {
        "binance": [lonely],
        "mexc": [instrument("mexc", "RAY_USDT", "RAY")],
        "gate": [instrument("gate", "RAY_USDT", "RAY")],
    }

    named = rename_lonely_contracts(catalog, market(binance__RAYSOL="2.15", mexc__RAY="2.1505", gate__RAY="2.1499"))

    assert [item.token for item in named["binance"]] == ["RAY"]
    assert [(a.symbol_raw, b.symbol_raw) for a, b in match_instruments(named["binance"], named["mexc"])] == [("RAYSOLUSDT", "RAY_USDT")]


def test_a_name_that_looks_alike_but_is_priced_differently_keeps_its_own():
    # SOLV is not SOL, whatever the spelling suggests, and the index prices say so.
    catalog = {
        "binance": [instrument("binance", "SOLVUSDT", "SOLV")],
        "mexc": [instrument("mexc", "SOL_USDT", "SOL")],
        "gate": [instrument("gate", "SOL_USDT", "SOL")],
    }

    named = rename_lonely_contracts(catalog, market(binance__SOLV="0.41", mexc__SOL="196.2", gate__SOL="196.3"))

    assert [item.token for item in named["binance"]] == ["SOLV"]


def test_a_contract_that_already_has_a_pair_is_never_renamed():
    catalog = {
        "binance": [instrument("binance", "RAYUSDT", "RAY")],
        "mexc": [instrument("mexc", "RAY_USDT", "RAY")],
        "gate": [instrument("gate", "RAYSOL_USDT", "RAYSOL")],
    }

    named = rename_lonely_contracts(catalog, market(binance__RAY="2.15", mexc__RAY="2.15", gate__RAYSOL="2.15"))

    assert [item.token for item in named["binance"]] == ["RAY"]
    assert [item.token for item in named["mexc"]] == ["RAY"]
    assert [item.token for item in named["gate"]] == ["RAY"]


def test_without_an_index_price_the_name_stands():
    catalog = {
        "binance": [instrument("binance", "RAYSOLUSDT", "RAYSOL")],
        "mexc": [instrument("mexc", "RAY_USDT", "RAY")],
        "gate": [instrument("gate", "RAY_USDT", "RAY")],
    }

    named = rename_lonely_contracts(catalog, market(mexc__RAY="2.15", gate__RAY="2.15"))

    assert [item.token for item in named["binance"]] == ["RAYSOL"]


def test_indices_that_merely_cross_are_not_the_same_coin():
    # gate's GIGGLEMAX and aster's MAX were 0.49 % apart by index and 5 % apart by price: different coins.
    catalog = {
        "gate": [instrument("gate", "GIGGLEMAX_USDT", "GIGGLEMAX")],
        "aster": [instrument("aster", "MAXUSDT", "MAX")],
        "mexc": [instrument("mexc", "MAX_USDT", "MAX")],
    }

    named = rename_lonely_contracts(catalog, market(gate__GIGGLEMAX="0.0070601", aster__MAX="0.0070256", mexc__MAX="0.0070260"))

    assert [item.token for item in named["gate"]] == ["GIGGLEMAX"]
