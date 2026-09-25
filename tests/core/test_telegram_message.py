"""The message is what the owner sees instead of the screen, so its wording is asserted, not eyeballed."""

from app.core.alerts import AlertLeg, GapAlert
from app.core.telegram_message import DigestNumbers, alarm, closed_note, digest, duration, gap, money, percent

NOW = 1_789_300_000_000


def alert(**overrides) -> GapAlert:
    values = dict(
        key="gate:LAPTOP_USDT|bingx:LAPTOP-USDT",
        token="LAPTOP",
        long=AlertLeg(exchange="gate", symbol="LAPTOP_USDT", url="https://gate.example/LAPTOP", price="0.021940"),
        short=AlertLeg(exchange="bingx", symbol="LAPTOP-USDT", url="https://bingx.example/LAPTOP", price="0.022310"),
        total_pct="1.6700",
        profit_pct="1.5900",
        profit_usd="1.59",
        interest=73,
        size_usd="100.00",
        capacity_usd="3400",
        funding_horizon_pct="0.0800",
        funding_next_pct="0.0100",
        funding_next_ms=NOW + 134_000,
        volume24h_weak_usd="2100000",
        lifetime_ms=192_000,
        blocks=(),
    )
    values.update(overrides)
    return GapAlert(**values)


def test_money_and_percent_read_at_a_glance():
    assert money("2100000") == "$2.1M"
    assert money("3400") == "$3.4K"
    assert money("95") == "$95"
    assert money("1.586") == "$1.59"
    assert money(None) == "—"
    assert percent("1.6700") == "+1.67%"
    assert percent("-0.4200") == "−0.42%"
    assert percent(None) == "—"


def test_duration_switches_units_with_the_scale():
    assert duration(42_000) == "42 с"
    assert duration(192_000) == "3 мин 12 с"
    assert duration(120_000) == "2 мин"
    assert duration(7_500_000) == "2 ч 05 мин"
    assert duration(None) == "—"


def test_a_gap_message_carries_the_numbers_the_decision_needs():
    message = gap(alert(), NOW)

    assert "<b>LAPTOP</b> · итог <b>+1.67%</b> · интерес 73" in message.text
    assert "🟢 лонг GATE <code>0.021940</code>" in message.text
    assert "🔴 шорт BINGX <code>0.022310</code>" in message.text
    assert "💰 профит $1.59 на $100 · ёмкость $3.4K" in message.text
    assert "⏳ фандинг +0.08% за 8 ч · ближайший через 2 мин 14 с" in message.text
    assert "📊 объём слабой ноги $2.1M · живёт 3 мин 12 с" in message.text
    assert message.buttons == (("GATE ↗", "https://gate.example/LAPTOP"), ("BINGX ↗", "https://bingx.example/LAPTOP"))
    assert "🔒" not in message.text


def test_a_settlement_that_eats_the_gap_gets_its_own_warning():
    message = gap(alert(funding_horizon_pct="-0.4700", funding_next_pct="-0.4200", funding_next_ms=NOW + 360_000), NOW)

    assert "⚠️ через 6 мин расчёт фандинга: −0.42%" in message.text


def test_a_far_away_or_harmless_settlement_says_nothing_extra():
    far = gap(alert(funding_next_pct="-0.4200", funding_next_ms=NOW + 4 * 3600_000), NOW)
    harmless = gap(alert(funding_next_pct="0.0100"), NOW)

    assert "⚠️" not in far.text and "⚠️" not in harmless.text


def test_the_best_gap_of_the_day_is_marked():
    message = gap(alert(total_pct="4.1600", interest=88), NOW, best_of_day=True)

    assert message.text.startswith("🔥 <b>LAPTOP</b>")
    assert "<i>лучшее за сутки</i>" in message.text


def test_a_gap_that_cannot_be_clicked_says_why_instead_of_staying_silent():
    message = gap(alert(blocks=("trading_disabled", "keys_not_accepted", "balance_unknown:long")), NOW)

    assert "🔒 торговля выключена — это сигнал, не сделка" in message.text
    assert "ключи" not in message.text  # одна причина, самая главная, а не список из четырёх


def test_unknown_funding_is_said_out_loud_not_counted_as_zero():
    message = gap(alert(funding_horizon_pct=None, funding_next_pct=None, funding_next_ms=None), NOW)

    assert "⏳ фандинг неизвестен — итог посчитан без него" in message.text


def test_the_outcome_is_appended_to_the_message_that_announced_it():
    assert closed_note(420_000, "2.1000", "converged") == "✓ прожила 7 мин · максимум был +2.10% · цены сошлись"
    assert closed_note(60_000, None, "gone") == "✓ прожила 1 мин · ушла из ленты"


def test_the_daily_digest_reads_as_a_report():
    message = digest(
        DigestNumbers(
            day="26 сентября",
            gaps=37,
            best_token="MOO",
            best_total_pct="4.1600",
            best_lifetime_ms=720_000,
            median_lifetime_ms=160_000,
            converged=31,
            by_pair=(("GATE ↔ MEXC", 14), ("GATE ↔ BINGX", 9)),
            database_size="1.2 ГБ",
            uptime_ms=3 * 24 * 3600_000,
        )
    )

    assert "📊 <b>Сутки · 26 сентября</b>" in message.text
    assert "вилок выше порога — 37" in message.text
    assert "лучшая — MOO +4.16% (12 мин)" in message.text
    assert "сошлись сами — 31 из 37" in message.text
    assert "  GATE ↔ MEXC — 14" in message.text
    assert "база 1.2 ГБ · терминал работает 3 ч 00 мин" not in message.text  # трое суток, а не три часа
    assert "терминал работает 72 ч 00 мин" in message.text


def test_an_alarm_says_what_broke_in_words():
    assert alarm("exchange_down", "MEXC, 4 мин").text == "🔴 <b>нет связи с биржей</b>\nMEXC, 4 мин"
    assert alarm("db_connected", "").text == "🟢 <b>база снова на связи</b>"
