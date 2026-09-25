"""Contract tests on public answers recorded from Binance and MEXC on 13.09.2026 (tests/exchanges/fixtures)."""

import json
from decimal import Decimal
from pathlib import Path

from app.core.links import trade_url
from app.core.pairs import assess_pair, match_instruments
from app.core.qty import plan_quantity
from app.exchanges.binance import adapter as binance
from app.exchanges.mexc import adapter as mexc

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def binance_instruments():
    return {item.symbol_raw: item for item in binance.parse_instruments(load("binance_exchangeinfo"))}


def mexc_instruments():
    return {item.symbol_raw: item for item in mexc.parse_instruments(load("mexc_detail"))}


def test_binance_keeps_only_trading_usdt_perpetuals():
    assert set(binance_instruments()) == {"BTCUSDT", "1000PEPEUSDT", "1INCHUSDT", "1000000MOGUSDT", "ONEUSDT"}


def test_binance_multiplier_contract_units():
    pepe = binance_instruments()["1000PEPEUSDT"]
    assert (pepe.token, pepe.qty_unit_tokens, pepe.price_unit_tokens) == ("PEPE", 1000, 1000)
    assert (pepe.qty_step_units, pepe.min_qty_units, pepe.max_market_qty_units) == (1, 1, 100000000)
    assert pepe.min_notional_usd == 5
    assert pepe.price_tick == Decimal("0.0000001")
    assert binance_instruments()["1INCHUSDT"].token == "1INCH"


def test_mexc_keeps_open_usdt_perpetuals_and_marks_the_ones_the_api_refuses():
    items = mexc_instruments()

    # BTC_USDC is settled in USDC and never belongs here. EMBER is quoted but apiAllowed=false: its gaps are real
    # and can be taken by hand on the exchange, so it is kept and marked rather than dropped.
    assert set(items) == {"BTC_USDT", "PEPE_USDT", "1INCH_USDT", "1000000MOG_USDT", "ONE_USDT", "1000BONK_USDT", "EMBER_USDT"}
    assert items["EMBER_USDT"].api_tradable is False
    assert all(items[symbol].api_tradable for symbol in items if symbol != "EMBER_USDT")


def test_mexc_contract_size_and_prefix_multiplier_become_tokens():
    items = mexc_instruments()
    assert items["PEPE_USDT"].qty_unit_tokens == 10_000_000
    assert items["PEPE_USDT"].price_unit_tokens == 1
    bonk = items["1000BONK_USDT"]
    assert (bonk.token, bonk.qty_unit_tokens, bonk.price_unit_tokens) == ("BONK", 10_000_000, 1000)
    assert bonk.min_notional_usd == 0


def test_mexc_best_prices_come_from_bid1_ask1_not_price_limits():
    raw = load("mexc_ticker")
    btc_raw = next(item for item in raw["data"] if item["symbol"] == "BTC_USDT")
    quote = mexc.parse_quotes(raw)["BTC_USDT"]
    assert quote.bid == Decimal(str(btc_raw["bid1"]))
    assert quote.ask == Decimal(str(btc_raw["ask1"]))
    assert quote.bid != Decimal(str(btc_raw["maxBidPrice"]))


def test_recorded_pairs_match_and_multipliers_agree_per_token():
    b_quotes = binance.parse_quotes(load("binance_premiumindex"), load("binance_bookticker"), load("binance_ticker24h"))
    m_quotes = mexc.parse_quotes(load("mexc_ticker"))
    pairs = match_instruments(list(binance_instruments().values()), list(mexc_instruments().values()))

    assessed = {a.token: assess_pair(a, b, b_quotes.get(a.symbol_raw), m_quotes.get(b.symbol_raw)) for a, b in pairs}

    assert set(assessed) == {"BTC", "PEPE", "1INCH", "MOG", "ONE"}
    assert assessed["PEPE"].price_gap_pct < 1
    assert assessed["MOG"].price_gap_pct < 1
    assert assessed["BTC"].suspicious_reason is None
    # On the recording day ONE's index prices disagreed by 4.7 % while both books stood at the same
    # 0.00068065 — the same asset with one bad index feed, not two coins under one ticker.
    assert assessed["ONE"].index_gap_pct > Decimal("4.7")
    assert assessed["ONE"].price_gap_pct == 0
    assert assessed["ONE"].suspicious_reason is None


def test_trade_links_point_at_the_pages_the_exchanges_serve_today():
    # MEXC retired futures.mexc.com/exchange/<symbol>: it redirects to plain http, where Akamai answers
    # "Access Denied", and loses the symbol on the way. Checked in a browser 22.09.2026.
    assert trade_url(mexc_instruments()["PEPE_USDT"]) == "https://www.mexc.com/futures/PEPE_USDT"
    assert trade_url(binance_instruments()["1000PEPEUSDT"]) == "https://www.binance.com/en/futures/1000PEPEUSDT"


def test_one_token_count_is_valid_on_both_exchanges_for_pepe():
    pepe_binance = binance_instruments()["1000PEPEUSDT"]
    pepe_mexc = mexc_instruments()["PEPE_USDT"]
    price = Decimal("0.00001")

    plan = plan_quantity(Decimal("1000"), price, long=pepe_mexc, short=pepe_binance)

    assert plan.qty_tokens == 100_000_000
    assert plan.units_long == 10  # MEXC contracts
    assert plan.units_short == 100_000  # Binance lots of 1000 PEPE
