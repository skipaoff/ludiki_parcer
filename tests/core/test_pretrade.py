import re
from dataclasses import replace

from app.core.pretrade import OpenFacts, RiskLimits, client_order_id, open_blocks
from tests.core.helpers import D

LIMITS = RiskLimits(max_open_pairs=3, max_total_usd=D("5000"), one_pair_per_token=True, margin_buffer_pct=D("20"), entry_min_roi_pct=D("0.5"))

GOOD = OpenFacts(
    trading_enabled=True,
    token="SOL",
    tradable_pair=True,
    both_legs_fresh=True,
    roi_net_pct=D("0.8"),
    qty_problem=None,
    notional_usd=D("1000"),
    available_long_usd=D("500"),
    available_short_usd=D("500"),
    leverage_long=3,
    leverage_short=3,
    open_pairs=1,
    open_tokens=frozenset({"BTC"}),
    open_notional_usd=D("2000"),
    keys_accepted=(True, True),
    exchange_blocked=(None, None),
    warmed_up=True,
    busy=False,
)


def test_clean_facts_pass():
    assert open_blocks(GOOD, LIMITS) == []


def test_every_failed_check_is_reported_together():
    bad = replace(
        GOOD,
        trading_enabled=False,
        both_legs_fresh=False,
        roi_net_pct=D("0.3"),
        open_pairs=3,
        open_tokens=frozenset({"SOL"}),
        open_notional_usd=D("4500"),
        available_long_usd=D("350"),
        warmed_up=False,
    )
    assert open_blocks(bad, LIMITS) == [
        "trading_disabled",
        "stale",
        "roi_below_entry",
        "max_open_pairs",
        "max_total_usd",
        "token_already_open",
        "insufficient_margin:long",
        "not_warmed_up",
    ]


def test_margin_includes_the_buffer():
    # $1000 at x3 needs $333.33 plus 20 % = $400.
    assert open_blocks(replace(GOOD, available_short_usd=D("400")), LIMITS) == []
    assert open_blocks(replace(GOOD, available_short_usd=D("399.99")), LIMITS) == ["insufficient_margin:short"]
    assert open_blocks(replace(GOOD, available_short_usd=None), LIMITS) == ["balance_unknown:short"]


def test_client_order_ids_are_unique_short_and_valid_for_both_exchanges():
    trade_id = 1789300000000 << 12 | 17
    ids = {client_order_id(trade_id, purpose, attempt) for purpose in ("open_long", "open_short", "close_long", "close_short", "fix_leg") for attempt in range(4)}
    assert len(ids) == 20
    for value in ids:
        assert len(value) <= 32
        assert re.fullmatch(r"[.A-Z:/a-z0-9_-]{1,36}", value)
