"""A gap worth taking should reach the user once — not every tick, and not when nobody could take it."""

from app.core.alerts import AlertRules, decide

RULES = AlertRules(cooldown_ms=600_000)


def row(key="binance:SOLUSDT|mexc:SOL_USDT", blocks=(), block=None, token="SOL"):
    return {
        "key": key,
        "token": token,
        "long": {"exchange": "mexc", "symbol": "SOL_USDT"},
        "short": {"exchange": "binance", "symbol": "SOLUSDT"},
        "total_pct": "1.4000",
        "roi_net_pct": "1.3000",
        "score": 64,
        "size_usd": "100.00",
        "block": block,
        "open_blocks": list(blocks),
    }


def test_a_clickable_gap_is_announced_once_and_carries_what_the_row_shows():
    alerts, announced = decide([row()], {}, now_ms=1_000, rules=RULES)

    assert [alert.token for alert in alerts] == ["SOL"]
    assert (alerts[0].long_exchange, alerts[0].short_exchange) == ("mexc", "binance")
    assert (alerts[0].total_pct, alerts[0].interest, alerts[0].size_usd) == ("1.4000", 64, "100.00")

    again, _ = decide([row()], announced, now_ms=2_000, rules=RULES)
    assert again == []


def test_the_same_gap_may_be_announced_again_after_the_cooldown():
    _, announced = decide([row()], {}, now_ms=1_000, rules=RULES)

    assert decide([row()], announced, now_ms=1_000 + RULES.cooldown_ms - 1, rules=RULES)[0] == []
    assert decide([row()], announced, now_ms=1_000 + RULES.cooldown_ms, rules=RULES)[0] != []


def test_trading_being_off_still_announces_the_gap_but_says_why_it_cannot_be_clicked():
    alerts, _ = decide([row(blocks=["trading_disabled", "keys_not_accepted"])], {}, now_ms=1_000, rules=RULES)

    assert len(alerts) == 1 and alerts[0].blocks == ("trading_disabled", "keys_not_accepted")


def test_a_gap_the_market_blocks_is_silent():
    for blocks in (["stale"], ["book_too_thin"], ["exchange_read_only:variational"], ["manual_only"], ["insufficient_margin:mexc"]):
        assert decide([row(blocks=blocks)], {}, now_ms=1_000, rules=RULES)[0] == []
    assert decide([row(block="suspicious")], {}, now_ms=1_000, rules=RULES)[0] == []


def test_announced_keys_do_not_pile_up_forever():
    old = {"gone:pair": 1_000}
    _, announced = decide([row()], old, now_ms=1_000 + RULES.cooldown_ms * 2, rules=RULES)

    assert "gone:pair" not in announced
