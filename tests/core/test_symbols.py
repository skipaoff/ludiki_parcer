from decimal import Decimal

import pytest

from app.core.symbols import ParsedSymbol, parse_symbol, price_gap_pct, same_asset_verdict


@pytest.mark.parametrize(
    ("raw", "exchange", "token", "multiplier"),
    [
        ("BTCUSDT", "binance", "BTC", 1),
        ("BTC_USDT", "mexc", "BTC", 1),
        ("BTC-USDT-SWAP", "okx", "BTC", 1),
        ("XBTUSDTM", "kucoin", "BTC", 1),
        ("1000PEPEUSDT", "binance", "PEPE", 1_000),
        ("1000000MOGUSDT", "binance", "MOG", 1_000_000),
        ("1MBABYDOGEUSDT", "binance", "BABYDOGE", 1_000_000),
        ("kPEPE", "hyperliquid", "PEPE", 1_000),
        ("KAVA", "hyperliquid", "KAVA", 1),
        ("1INCHUSDT", "binance", "1INCH", 1),
    ],
)
def test_parse_symbol_keeps_multiplier_contracts(raw, exchange, token, multiplier):
    assert parse_symbol(raw, exchange) == ParsedSymbol(token, Decimal(multiplier))


@pytest.mark.parametrize(
    ("raw", "exchange", "token"),
    [
        # The same stock, written both ways: MEXC adds the suffix, the other three do not.
        ("AAPLSTOCK_USDT", "mexc", "AAPL"),
        ("AAPLUSDT", "bybit", "AAPL"),
        ("SITMSTOCK_USDT", "mexc", "SITM"),
        ("SITMUSDT", "bitget", "SITM"),
        ("PENGSTOCKUSDT", "bybit", "PENG"),
        # A coin whose own name ends there keeps it: the stem has to be a name of its own.
        ("STOCKUSDT", "binance", "STOCK"),
    ],
)
def test_the_stock_suffix_is_not_part_of_the_name(raw, exchange, token):
    assert parse_symbol(raw, exchange) == ParsedSymbol(token, Decimal(1))


@pytest.mark.parametrize(
    ("raw", "exchange"),
    [("BTCUSDC", "binance"), ("BTCUSD", "bybit"), (".BTCUSDT", "binance"), ("@107", "hyperliquid"), ("USDT", "binance")],
)
def test_parse_symbol_rejects_non_usdt_and_service_names(raw, exchange):
    assert parse_symbol(raw, exchange) is None


def test_price_gap_pct_is_relative_to_lower_price():
    assert price_gap_pct(Decimal("100"), Decimal("103")) == Decimal("3")
    assert price_gap_pct(Decimal("103"), Decimal("100")) == Decimal("3")


def test_same_asset_verdict_flags_index_mismatch_and_missing_index():
    limit = Decimal("1")
    assert same_asset_verdict(Decimal("1.000"), Decimal("1.005"), limit) is None
    assert same_asset_verdict(Decimal("1.00"), Decimal("1.40"), limit) == "index_mismatch"
    assert same_asset_verdict(None, Decimal("1.0"), limit) == "index_missing"


def test_prices_on_top_of_each_other_overrule_a_broken_index():
    # BingX published an index 31 % away from its own ONE price, which traded within 0.14 % of Binance's.
    limit = Decimal("1")
    verdict = same_asset_verdict(Decimal("0.0018"), Decimal("0.00236"), limit, Decimal("0.0022486"), Decimal("0.0022517"))

    assert verdict is None


def test_an_index_mismatch_stands_when_the_prices_disagree_too():
    limit = Decimal("1")
    verdict = same_asset_verdict(Decimal("0.001291"), Decimal("0.001244"), limit, Decimal("0.00124"), Decimal("0.001261"))

    assert verdict == "index_mismatch"


def test_missing_prices_leave_the_indices_to_decide():
    limit = Decimal("1")
    assert same_asset_verdict(Decimal("1.00"), Decimal("1.40"), limit, None, None) == "index_mismatch"
