"""Gate and Variational on public answers recorded 14.09.2026; Gate private shapes follow the documentation."""

import json
from decimal import Decimal
from pathlib import Path

import orjson
import pytest

from app.core.legs import OrderOutcome
from app.core.pairs import assess_pair, match_all
from app.core.qty import plan_quantity
from app.core.schemas import Fees, LegSide
from app.core.vwap import walk
from app.exchanges.gate import adapter as gate
from app.exchanges.variational import adapter as variational
from app.market import gate_market
from app.market.state import MarketState
from app.market.variational_market import apply_stats
from tests.core.helpers import instrument

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def gate_instruments():
    return {item.symbol_raw: item for item in gate.parse_instruments(load("gate_contracts"))}


def test_gate_contract_multiplier_is_tokens_per_contract():
    items = gate_instruments()
    assert set(items) == {"BTC_USDT", "ETH_USDT", "SOL_USDT", "PEPE_USDT", "1INCH_USDT"}
    btc, pepe = items["BTC_USDT"], items["PEPE_USDT"]
    assert (btc.token, btc.qty_unit_tokens, btc.price_unit_tokens, btc.qty_step_units) == ("BTC", Decimal("0.0001"), 1, 1)
    assert pepe.qty_unit_tokens == Decimal("10000000")
    assert pepe.min_qty_units == 1  # order_size_min 0 with decimal sizes: still one whole contract
    assert btc.max_market_qty_units == Decimal("10000000")
    assert items["1INCH_USDT"].token == "1INCH"


def test_gate_skips_contracts_that_are_not_trading():
    delisting = {**load("gate_contracts")[0], "name": "OLD_USDT", "in_delisting": True}
    pre_market = {**load("gate_contracts")[0], "name": "NEW_USDT", "is_pre_market": True}
    assert gate.parse_instruments([delisting, pre_market]) == []


def test_gate_tickers_give_best_prices_index_and_turnover():
    raw = load("gate_tickers")
    btc = next(item for item in raw if item["contract"] == "BTC_USDT")
    quote = gate.parse_quotes(raw)["BTC_USDT"]
    assert (quote.bid, quote.ask) == (Decimal(btc["highest_bid"]), Decimal(btc["lowest_ask"]))
    assert quote.index == Decimal(btc["index_price"]) and quote.volume24h_usd == Decimal(btc["volume_24h_quote"])


def test_gate_book_frame_sizes_are_contracts():
    state = MarketState(lambda: 1.0)
    state.set_instruments([gate_instruments()["PEPE_USDT"]])
    frame = {
        "time": 1789371993, "time_ms": 1789371993681, "channel": "futures.order_book", "event": "all",
        "result": {"t": 1789371993671, "id": 1, "contract": "PEPE_USDT", "asks": [{"p": "0.000003444", "s": 12}], "bids": [{"p": "0.000003443", "s": 7}]},
    }
    gate_market.handle_frame(state, orjson.dumps(frame))
    book = state.books[("gate", "PEPE_USDT")]
    assert book.asks[0].qty_tokens == Decimal("120000000")
    assert book.bids[0].price == Decimal("0.000003443")
    assert [orjson.loads(m)["payload"] for m in gate_market.depth_messages(["BTC_USDT"], True)] == [["BTC_USDT", "20", "0"]]


@pytest.mark.parametrize(
    ("status", "finish_as", "size", "left", "outcome"),
    [
        ("finished", "filled", 30, 0, OrderOutcome.FILLED),
        ("finished", "ioc", 30, 12, OrderOutcome.PARTIAL),
        ("finished", "ioc", -30, -30, OrderOutcome.REJECTED),
        ("open", "_new", 30, 30, OrderOutcome.UNKNOWN),
    ],
)
def test_gate_order_outcomes(status, finish_as, size, left, outcome):
    sol = gate_instruments()["SOL_USDT"]
    raw = {"id": 15675394, "contract": "SOL_USDT", "size": size, "left": left, "fill_price": "143.1", "status": status,
           "finish_as": finish_as, "text": "t-lkabcol0", "tif": "ioc", "is_reduce_only": False}
    requested = Decimal(abs(size)) * sol.qty_unit_tokens
    report = gate.parse_order(raw, sol, "lkabcol0", requested, 1, 2)
    assert report.outcome is outcome
    assert report.filled_tokens == Decimal(abs(size) - abs(left)) * sol.qty_unit_tokens


def test_gate_positions_balance_funding_fees():
    sol = gate_instruments()["SOL_USDT"]
    positions = gate.parse_positions(
        [{"contract": "SOL_USDT", "size": -25, "leverage": "3", "entry_price": "143.1", "mark_price": "143.0", "liq_price": "190.2", "mode": "single", "update_time": 1789371993},
         {"contract": "SOL_USDT", "size": 0}],
        {"SOL_USDT": sol},
    )
    assert len(positions) == 1 and positions[0].side is LegSide.SHORT
    assert positions[0].qty_tokens == 25 * sol.qty_unit_tokens and positions[0].margin_mode == "isolated"
    balance = gate.parse_balance({"total": "1000.5", "unrealised_pnl": "-2.5", "available": "800", "position_margin": "150", "order_margin": "48"})
    assert (balance.equity_usd, balance.available_usd, balance.margin_used_usd) == (Decimal("998.0"), Decimal("800"), Decimal("198"))
    book = [{"time": 1789372800.0, "change": "-0.0123", "type": "fund", "text": "SOL_USDT:funding"},
            {"time": 1789372800.0, "change": "0.5", "type": "fund", "text": "BTC_USDT:funding"},
            {"time": 1789300000.0, "change": "1", "type": "fund", "text": "SOL_USDT:funding"}]
    assert gate.parse_funding(book, "SOL_USDT", since_ms=1789370000000) == Decimal("-0.0123")
    assert gate.parse_fee_rates({"BTC_USDT": {"taker_fee": "0.00075", "maker_fee": "-0.0001"}}) == (Decimal("0.07500"), Decimal("-0.0100"))
    assert gate.custom_text("lkabcol0") == "t-lkabcol0"


def test_variational_listings_multiplier_and_two_point_book():
    stats = load("variational_stats")
    items = {item.symbol_raw: item for item in variational.parse_instruments(stats)}
    pepe = items["1000PEPE"]
    assert (pepe.token, pepe.qty_unit_tokens, pepe.price_unit_tokens) == ("PEPE", 1000, 1000)

    listing = next(item for item in stats["listings"] if item["ticker"] == "POWER")
    small_ask, large_ask = Decimal(listing["quotes"]["size_1k"]["ask"]), Decimal(listing["quotes"]["size_100k"]["ask"])
    levels = variational.tier_levels(small_ask, large_ask)
    state = MarketState(lambda: 1_789_372_000_000.0)
    state.set_instruments([items["POWER"]])
    state.set_book("variational", "POWER", levels, levels, 0)
    asks = state.books[("variational", "POWER")].asks
    # Walking the synthetic book reproduces both quoted averages exactly.
    assert walk(asks, Decimal(1000) / small_ask).avg_price == small_ask
    assert abs(walk(asks, Decimal(100000) / large_ask).avg_price - large_ask) < Decimal("1e-20")
    assert walk(asks, Decimal(100000) / large_ask * 2) is None  # beyond $100k Variational quotes nothing


def test_variational_timestamp_keeps_its_utc_zone():
    # 2026-09-14T08:01:11.466Z == 1789372871466 ms; the nanosecond fraction must not swallow the zone.
    assert variational._updated_ms("2026-09-14T08:01:11.466733379Z") == 1789372871466
    assert variational._updated_ms("2026-09-14T08:01:11Z") == 1789372871000
    assert variational._updated_ms("2026-09-14T08:01:11.466") == 0
    assert variational._updated_ms("garbage") == 0


def test_variational_quotes_age_from_their_own_timestamp():
    stats = load("variational_stats")
    items = variational.parse_instruments(stats)
    btc_listing = next(item for item in stats["listings"] if item["ticker"] == "BTC")
    updated = variational._updated_ms(btc_listing["quotes"]["updated_at"])
    now = updated + 12_000
    state = MarketState(lambda: now)
    state.set_instruments(items)
    assert apply_stats(state, stats, now) >= 5
    assert state.book_age_ms(("variational", "BTC")) == 12_000
    assert state.marks[("variational", "BTC")].index is None


def test_variational_pair_uses_mark_against_the_other_index_and_needs_no_fees():
    stats = load("variational_stats")
    var_btc = next(item for item in variational.parse_instruments(stats) if item.symbol_raw == "BTC")
    gate_btc = gate_instruments()["BTC_USDT"]
    var_quote = variational.parse_quotes(stats)["BTC"]
    gate_quote = gate.parse_quotes(load("gate_tickers"))["BTC_USDT"]

    assessment = assess_pair(gate_btc, var_btc, gate_quote, var_quote)

    assert assessment.suspicious_reason is None
    assert assessment.index_gap_pct is not None and assessment.index_gap_pct < 1
    plan = plan_quantity(Decimal("1000"), Decimal("77760"), long=gate_btc, short=var_btc)
    assert plan.qty_tokens == Decimal("0.0128")  # Gate's 0.0001 BTC contract step decides


def test_pairs_across_every_two_exchanges_keep_a_stable_leg_order():
    binance_btc = instrument("binance", "BTCUSDT", "BTC")
    mexc_btc = instrument("mexc", "BTC_USDT", "BTC")
    gate_btc = gate_instruments()["BTC_USDT"]
    pairs = match_all({"gate": [gate_btc], "binance": [binance_btc], "mexc": [mexc_btc]}, ["binance", "mexc", "gate", "variational"])
    assert [(a.exchange, b.exchange) for a, b in pairs] == [("binance", "mexc"), ("binance", "gate"), ("mexc", "gate")]


def test_zero_fee_venue_changes_round_trip_fees():
    fees = Fees(taker_long_pct=Decimal("0.075"), taker_short_pct=Decimal("0"))
    assert fees.round_trip_pct == Decimal("0.150")
