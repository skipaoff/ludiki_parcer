from decimal import Decimal

from app.core.portfolio import (
    TradeLegs,
    entry_spread_pct,
    estimated_entry_fees_usd,
    pair_metrics,
    pair_quantity,
    reconcile,
    suggest_pairs,
)
from app.core.roi import ExitQuote
from app.core.schemas import LegSide
from app.exchanges.base import Position
from tests.core.helpers import D, fees


def position(exchange, symbol, token, side, qty, entry="100", liquidation=None) -> Position:
    return Position(
        exchange=exchange,
        symbol_raw=symbol,
        token=token,
        side=side,
        qty_tokens=D(qty),
        entry_price=D(entry),
        mark_price=None,
        liquidation_price=D(liquidation) if liquidation else None,
    )


SOL_TRADE = TradeLegs(7, "SOL", "mexc", "SOL_USDT", "binance", "SOLUSDT", D("10"))


def test_matching_legs_attach_to_their_trade_and_the_rest_is_foreign():
    long = position("mexc", "SOL_USDT", "SOL", LegSide.LONG, "10")
    short = position("binance", "SOLUSDT", "SOL", LegSide.SHORT, "10")
    manual = position("binance", "BTCUSDT", "BTC", LegSide.LONG, "0.01")

    result = reconcile([SOL_TRADE], [long, short, manual])

    assert result.legs[7] == (long, short)
    assert result.foreign == [manual]
    assert result.issues == {}


def test_missing_leg_and_quantity_mismatch_are_issues():
    short = position("binance", "SOLUSDT", "SOL", LegSide.SHORT, "9.5")
    result = reconcile([SOL_TRADE], [short])
    assert result.legs[7] == (None, short)
    assert result.issues[7] == ("long_missing", "short_qty_mismatch")


def test_wrong_side_is_not_the_leg():
    flipped = position("mexc", "SOL_USDT", "SOL", LegSide.SHORT, "10")
    result = reconcile([SOL_TRADE], [flipped])
    assert "long_missing" in result.issues[7]
    assert result.foreign == [flipped]


def test_foreign_legs_on_two_exchanges_suggest_a_pair():
    long = position("mexc", "PEPE_USDT", "PEPE", LegSide.LONG, "100000000")
    short = position("binance", "1000PEPEUSDT", "PEPE", LegSide.SHORT, "99000000")
    same_exchange = position("mexc", "BTC_USDT", "BTC", LegSide.LONG, "1")
    other_token = position("binance", "ETHUSDT", "ETH", LegSide.SHORT, "1")

    assert suggest_pairs([long, short, same_exchange, other_token]) == [(long, short)]
    assert pair_quantity(long, short, D("10000000")) == D("90000000")


def test_metrics_of_an_open_pair():
    exit = ExitQuote(qty_tokens=D("10"), long_exit_avg=D("101"), short_exit_avg=D("101.2"), exit_spread_pct=D("0.198"))
    taker = fees()
    entry_fees = estimated_entry_fees_usd(D("10"), D("100"), D("101.2"), taker)

    metrics = pair_metrics(
        entry_long=D("100"),
        entry_short=D("101.2"),
        exit=exit,
        fees=taker,
        entry_fees_usd=entry_fees,
        funding_usd=D("0.3"),
        mark_long=D("101"),
        liquidation_long=D("70"),
        mark_short=D("101"),
        liquidation_short=D("130"),
    )

    assert entry_fees == D("1.006")
    assert metrics.entry_spread_pct == entry_spread_pct(D("100"), D("101.2")) == D("1.2")
    # long +10, short 0, fees in 1.006 + out 1.011, funding +0.3
    assert metrics.pnl_now_usd == D("10") - D("1.006") - D("1.011") + D("0.3")
    assert metrics.worst_liquidation_pct == Decimal("28.71287128712871287128712871")


def test_metrics_without_books_or_marks_stay_unknown():
    metrics = pair_metrics(D("100"), D("101"), None, fees(), D("0"), D("0"), None, None, None, None)
    assert metrics.pnl_now_usd is None and metrics.exit_spread_pct is None and metrics.worst_liquidation_pct is None
