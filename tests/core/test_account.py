from decimal import Decimal

from app.core.account import AccountFacts, KeyPermissions, clock_offset_ms, judge


def good_facts(**overrides) -> AccountFacts:
    values = dict(
        permissions=KeyPermissions(reading=True, futures=True, withdrawals=False, ip_restricted=True),
        one_way_position_mode=True,
        wallet_usdt=Decimal("500"),
        available_usdt=Decimal("450"),
        taker_fee_pct=Decimal("0.05"),
        maker_fee_pct=Decimal("0.02"),
        ping_ms=300,
        clock_offset_ms=-40,
    )
    values.update(overrides)
    return AccountFacts(**values)


def test_clean_account_is_accepted_without_warnings():
    verdict = judge(good_facts())
    assert verdict.accepted
    assert verdict.warnings == ()


def test_withdrawal_permission_always_blocks():
    permissions = KeyPermissions(reading=True, futures=True, withdrawals=True, ip_restricted=True)
    verdict = judge(good_facts(permissions=permissions))
    assert not verdict.accepted
    assert "withdrawals_enabled" in verdict.blocking


def test_hedge_mode_and_disabled_futures_block():
    permissions = KeyPermissions(reading=True, futures=False, withdrawals=False, ip_restricted=True)
    verdict = judge(good_facts(permissions=permissions, one_way_position_mode=False))
    assert set(verdict.blocking) == {"futures_disabled", "hedge_mode"}


def test_unknown_facts_become_warnings_not_passes():
    # MEXC does not report key permissions for futures keys.
    verdict = judge(good_facts(permissions=KeyPermissions(), taker_fee_pct=None))
    assert verdict.accepted
    assert set(verdict.warnings) == {"withdrawals_unknown", "fees_unknown"}


def test_failed_step_blocks_until_a_clean_check():
    verdict = judge(good_facts(errors=("balance: AuthenticationError",)))
    assert verdict.blocking == ("check_incomplete",)


def test_soft_problems_are_warnings():
    permissions = KeyPermissions(reading=True, futures=True, withdrawals=False, ip_restricted=False)
    verdict = judge(good_facts(permissions=permissions, available_usdt=Decimal(0), clock_offset_ms=1500))
    assert verdict.accepted
    assert set(verdict.warnings) == {"no_ip_restriction", "no_free_balance", "clock_offset"}


def test_clock_offset_uses_the_middle_of_the_round_trip():
    assert clock_offset_ms(sent_ms=1000, received_ms=1300, server_ms=1200) == 50
    assert clock_offset_ms(sent_ms=1000, received_ms=1300, server_ms=1000) == -150
