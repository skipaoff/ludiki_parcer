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


def test_a_terminal_without_keys_still_announces_gaps():
    """Without keys every row carries balance_unknown; treating it as a market block would silence the terminal."""
    blocks = ["trading_disabled", "keys_not_accepted", "balance_unknown:long", "balance_unknown:short", "not_warmed_up"]

    assert len(decide([row(blocks=blocks)], {}, now_ms=1_000, rules=RULES)[0]) == 1


def test_a_gap_the_market_blocks_is_silent():
    for blocks in (["stale"], ["book_too_thin"], ["exchange_read_only:variational"], ["manual_only"], ["insufficient_margin:mexc"]):
        assert decide([row(blocks=blocks)], {}, now_ms=1_000, rules=RULES)[0] == []
    assert decide([row(block="suspicious")], {}, now_ms=1_000, rules=RULES)[0] == []


def test_announced_keys_do_not_pile_up_forever():
    old = {"gone:pair": 1_000}
    _, announced = decide([row()], old, now_ms=1_000 + RULES.cooldown_ms * 2, rules=RULES)

    assert "gone:pair" not in announced


def live(key="binance:SOLUSDT|mexc:SOL_USDT", announced_ms=1_000, peak=None, missing=None):
    from app.core.alerts import LiveAlert

    return {key: LiveAlert(key, announced_ms, peak, missing)}


def test_an_announced_gap_keeps_its_best_result_while_it_is_on_screen():
    from app.core.alerts import track

    updated, finished = track(live(), [row(**{"key": "binance:SOLUSDT|mexc:SOL_USDT"}) | {"total_pct": "1.9000"}], 2_000)

    assert finished == []
    assert updated["binance:SOLUSDT|mexc:SOL_USDT"].peak_total_pct == "1.9000"

    updated, _ = track(updated, [row() | {"total_pct": "1.2000"}], 3_000)
    assert updated["binance:SOLUSDT|mexc:SOL_USDT"].peak_total_pct == "1.9000"  # пик, а не последнее значение


def test_a_gap_blinking_out_for_a_tick_is_not_over():
    from app.core.alerts import track

    updated, finished = track(live(), [], 2_000, gone_after_ms=60_000)

    assert finished == [] and updated["binance:SOLUSDT|mexc:SOL_USDT"].missing_since_ms == 2_000

    back, finished = track(updated, [row()], 3_000)
    assert finished == [] and back["binance:SOLUSDT|mexc:SOL_USDT"].missing_since_ms is None


def test_a_gap_gone_for_good_is_reported_once_with_its_lifetime():
    from app.core.alerts import track

    updated, _ = track(live(announced_ms=1_000, peak="2.1000"), [], 10_000, gone_after_ms=60_000)
    updated, finished = track(updated, [], 70_000, gone_after_ms=60_000)

    assert updated == {}
    assert finished[0].lifetime_ms == 9_000 and finished[0].peak_total_pct == "2.1000"


def test_the_phone_gate_applies_the_same_filters_as_the_screen():
    """Каждая мера отдельной проверкой: сообщение — это внимание человека, а не строка в таблице."""
    from decimal import Decimal

    from app.core.alerts import PhoneGate, passes

    gap = decide([row() | {"volume24h_weak_usd": "2100000", "capacity_usd": "3400"}], {}, now_ms=1_000, rules=RULES)[0][0]

    assert passes(gap)
    assert passes(gap, PhoneGate(min_interest=64)) and not passes(gap, PhoneGate(min_interest=65))
    assert passes(gap, PhoneGate(min_total_pct=Decimal("1.4"))) and not passes(gap, PhoneGate(min_total_pct=Decimal("1.5")))
    assert passes(gap, PhoneGate(max_total_pct=Decimal("1.4"))) and not passes(gap, PhoneGate(max_total_pct=Decimal("1.3")))
    assert passes(gap, PhoneGate(min_volume24h_usd=Decimal("2000000")))
    assert not passes(gap, PhoneGate(min_volume24h_usd=Decimal("3000000")))
    assert passes(gap, PhoneGate(min_capacity_usd=Decimal("3400")))
    assert not passes(gap, PhoneGate(min_capacity_usd=Decimal("3401")))


def test_a_gap_without_the_numbers_a_filter_needs_does_not_slip_through_it():
    from decimal import Decimal

    from app.core.alerts import PhoneGate, passes

    gap = decide([row()], {}, now_ms=1_000, rules=RULES)[0][0]

    assert gap.volume24h_weak_usd is None and gap.capacity_usd is None
    assert not passes(gap, PhoneGate(min_volume24h_usd=Decimal("1")))
    assert not passes(gap, PhoneGate(min_capacity_usd=Decimal("1")))
