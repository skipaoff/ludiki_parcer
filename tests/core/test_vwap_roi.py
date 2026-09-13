from decimal import Decimal

import pytest

from app.core.roi import (
    best_entry,
    capacity_tokens,
    entry_quote,
    exit_quote,
    liquidation_distance_pct,
    pnl_now_usd,
    top_of_book_roi_net,
)
from app.core.vwap import walk
from tests.core.helpers import D, book, fees, levels


def test_walk_averages_across_levels():
    fill = walk(levels(("1.00", "100"), ("1.02", "100")), D("150"))
    assert fill.avg_price == (D("100") * D("1.00") + D("50") * D("1.02")) / D("150")
    assert fill.worst_price == D("1.02")
    assert fill.levels_used == 2


def test_walk_returns_none_when_depth_is_insufficient():
    assert walk(levels(("1.00", "10")), D("11")) is None


def test_walk_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        walk(levels(("1.00", "10")), D("0"))


def test_plan_example_entry_roi():
    # docs/PLAN.md 7.2: long MEXC avg ask 1.0040, short Binance avg bid 1.0180, taker 0.05% on both.
    mexc = book("mexc", asks=levels(("1.0040", "1000")))
    binance = book("binance", bids=levels(("1.0180", "1000")))

    quote = entry_quote(mexc, binance, D("996"), fees())

    assert quote.long_exchange == "mexc"
    assert round(quote.roi_gross_pct, 3) == D("1.394")
    assert round(quote.roi_net_pct, 3) == D("1.194")


def test_thin_book_kills_a_gap_that_looks_good_on_top_of_book():
    mexc = book("mexc", asks=levels(("1.000", "10"), ("1.030", "990")))
    binance = book("binance", bids=levels(("1.020", "1000")))

    assert top_of_book_roi_net(mexc, binance, fees()) == D("1.80")
    assert entry_quote(mexc, binance, D("1000"), fees()).roi_net_pct < 0


def test_best_entry_picks_the_profitable_direction():
    a = book("binance", bids=levels(("101", "10")), asks=levels(("101.1", "10")))
    b = book("mexc", bids=levels(("99.9", "10")), asks=levels(("100", "10")))

    quote = best_entry(a, b, D("5"), fees(), fees())

    assert (quote.long_exchange, quote.short_exchange) == ("mexc", "binance")


def test_capacity_is_the_largest_step_multiple_above_threshold():
    asks = levels(("1.00", "100"), ("1.01", "100"), ("1.05", "1000"))
    bids = levels(("1.03", "1000"))

    qty = capacity_tokens(asks, bids, fees(), min_roi_net_pct=D("1.5"), step_tokens=D("10"))

    # 240 tokens: avg ask 1.0125, net 1.53%; 250 tokens: avg ask 1.014, net 1.38%.
    assert qty == D("240")
    assert entry_quote(book("l", asks=asks), book("s", bids=bids), qty, fees()).roi_net_pct >= D("1.5")
    assert entry_quote(book("l", asks=asks), book("s", bids=bids), qty + 10, fees()).roi_net_pct < D("1.5")


def test_capacity_is_zero_when_one_step_fails():
    assert capacity_tokens(levels(("1.00", "100")), levels(("1.001", "100")), fees(), D("0.5"), D("1")) == 0


def test_exit_spread_and_pnl_now():
    long_book = book("mexc", bids=levels(("1.0100", "1000")))
    short_book = book("binance", asks=levels(("1.0120", "1000")))

    exit = exit_quote(long_book, short_book, D("1000"))
    pnl = pnl_now_usd(
        entry_long_avg=D("1.0040"),
        entry_short_avg=D("1.0180"),
        exit=exit,
        fees=fees(),
        entry_fees_paid_usd=D("1.011"),
        funding_net_usd=D("-0.10"),
    )

    assert round(exit.exit_spread_pct, 4) == D("0.1980")
    # long +6.00, short +6.00, entry fees 1.011, exit fees (1010 + 1012) * 0.05% = 1.011, funding -0.10
    assert pnl == D("12.00") - D("1.011") - D("1.011") - D("0.10")


def test_liquidation_distance():
    assert liquidation_distance_pct(D("100"), D("82")) == D("18")
    assert liquidation_distance_pct(D("100"), None) is None
