import pytest

from app.core.radar import best_price_roi, choose_books, leg_fresh


@pytest.mark.parametrize(
    ("book_age", "stream_age", "fresh"),
    [
        (300, None, True),  # recent change
        (4_000, 200, True),  # quiet book, connection alive
        (4_000, 3_000, False),  # connection silent too
        (12_000, 100, False),  # quiet for too long, trust it no more
        (None, 100, False),  # no book yet
    ],
)
def test_leg_freshness(book_age, stream_age, fresh):
    assert leg_fresh(book_age, stream_age, fresh_ms=1_000, quiet_book_max_ms=10_000) is fresh


def test_direction_with_the_cheaper_ask_goes_long():
    roi = best_price_roi("P", "binance", bid_a=100.0, ask_a=100.1, exchange_b="mexc", bid_b=101.2, ask_b=101.3, round_trip_fee_pct=0.2)
    assert (roi.long_exchange, roi.short_exchange) == ("binance", "mexc")
    assert roi.roi_net_pct == pytest.approx((101.2 - 100.1) / 100.1 * 100 - 0.2)

    mirrored = best_price_roi("P", "binance", 101.2, 101.3, "mexc", 100.0, 100.1, 0.2)
    assert (mirrored.long_exchange, mirrored.short_exchange) == ("mexc", "binance")


def test_normal_spread_is_negative_after_fees():
    roi = best_price_roi("BTC", "binance", 77216.9, 77217.0, "mexc", 77222.0, 77222.1, 0.2)
    assert roi.roi_net_pct < 0


def test_missing_price_gives_nothing():
    assert best_price_roi("X", "binance", 0.0, 1.0, "mexc", 1.0, 1.0, 0.2) is None


def test_must_keep_first_then_young_subscriptions_then_ranked():
    current = {"old": 0, "young": 9_000}
    chosen = choose_books(
        must_keep=["feed1"],
        ranked=["best", "old", "second"],
        current=current,
        now_ms=10_000,
        limit=3,
        min_hold_ms=5_000,
    )
    assert list(chosen) == ["feed1", "young", "best"]
    assert chosen["young"] == 9_000
    assert chosen["best"] == 10_000


def test_old_subscription_survives_when_it_is_still_ranked():
    chosen = choose_books([], ["old", "new"], {"old": 0}, now_ms=60_000, limit=2, min_hold_ms=5_000)
    assert chosen == {"old": 0, "new": 60_000}


def test_limit_is_never_exceeded_even_by_must_keep():
    chosen = choose_books(["a", "b", "c"], ["d"], {}, now_ms=0, limit=2, min_hold_ms=0)
    assert list(chosen) == ["a", "b"]
