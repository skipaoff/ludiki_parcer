"""
VFP: The exact text of every message the terminal sends to Telegram, and the buttons under it.
Changes when: the wording or the set of numbers in a message changes.
Anti-goal:
1. Computing anything about a gap here — the numbers arrive ready; this file only chooses words.
2. Sending: no network, no clock of its own, so every message can be asserted in a test.
3. A message that hides why a gap cannot be taken — the reason is part of the text.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.core.alerts import GapAlert

SOON_MS = 30 * 60 * 1000
"""A settlement this close is worth a warning line of its own."""
BAD_SETTLEMENT_PCT = Decimal("-0.05")

# In the order that answers "why can't I click this?" first. Only the first reason that applies is shown:
# a terminal in watch mode would otherwise repeat four of them under every single gap.
BLOCK_WORDS = (
    ("trading_disabled", "торговля выключена — это сигнал, не сделка"),
    ("keys_not_accepted", "ключи биржи не добавлены"),
    ("max_open_pairs", "достигнут лимит открытых пар"),
    ("max_total_usd", "достигнут лимит общего объёма"),
    ("balance_unknown", "баланс биржи неизвестен"),
    ("not_warmed_up", "плечо ещё не выставлено"),
)


@dataclass(frozen=True, slots=True)
class Message:
    text: str
    buttons: tuple[tuple[str, str], ...] = ()
    """(подпись, ссылка) — по кнопке на каждую ногу."""


def _number(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def money(value: str | Decimal | None) -> str:
    """$1.2M, $3.4K, $95 — деньги читаются с одного взгляда, точность тут не нужна."""
    number = value if isinstance(value, Decimal) else _number(value)
    if number is None:
        return "—"
    amount = abs(number)
    sign = "−" if number < 0 else ""
    if amount >= 1_000_000:
        return f"{sign}${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{sign}${amount / 1_000:.1f}K"
    if amount >= 10:
        return f"{sign}${amount:.0f}"
    return f"{sign}${amount:.2f}"


def percent(value: str | Decimal | None, digits: int = 2) -> str:
    number = value if isinstance(value, Decimal) else _number(value)
    if number is None:
        return "—"
    sign = "+" if number > 0 else ("−" if number < 0 else "")
    return f"{sign}{abs(number):.{digits}f}%"


def price(value: str | None) -> str:
    return value if value else "—"


def duration(ms: int | None) -> str:
    if ms is None or ms < 0:
        return "—"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {rest:02d} с" if rest else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def _venue(name: str | None) -> str:
    return html.escape((name or "?").upper())


def gap(alert: GapAlert, now_ms: int, horizon_h: str = "8", best_of_day: bool = False) -> Message:
    """The message for one gap that just became takeable."""
    token = html.escape(alert.token)
    head = "🔥" if best_of_day else "🔀"
    interest = "—" if alert.interest is None else str(alert.interest)
    lines = [f"{head} <b>{token}</b> · итог <b>{percent(alert.total_pct)}</b> · интерес {interest}"]
    if best_of_day:
        lines.append("<i>лучшее за сутки</i>")
    lines.append("")
    lines.append(f"🟢 лонг {_venue(alert.long.exchange)} <code>{price(alert.long.price)}</code>")
    lines.append(f"🔴 шорт {_venue(alert.short.exchange)} <code>{price(alert.short.price)}</code>")
    lines.append("")

    size = money(alert.size_usd)
    capacity = f" · ёмкость {money(alert.capacity_usd)}" if alert.capacity_usd else ""
    lines.append(f"💰 профит {money(alert.profit_usd)} на {size}{capacity}")

    if alert.funding_horizon_pct is not None:
        settlement = ""
        if alert.funding_next_ms:
            settlement = f" · ближайший через {duration(alert.funding_next_ms - now_ms)}"
        lines.append(f"⏳ фандинг {percent(alert.funding_horizon_pct)} за {horizon_h} ч{settlement}")
    else:
        lines.append("⏳ фандинг неизвестен — итог посчитан без него")

    next_pct = _number(alert.funding_next_pct)
    if next_pct is not None and next_pct <= BAD_SETTLEMENT_PCT and alert.funding_next_ms:
        away = alert.funding_next_ms - now_ms
        if 0 <= away <= SOON_MS:
            lines.append(f"⚠️ через {duration(away)} расчёт фандинга: {percent(next_pct)}")

    volume = f"объём слабой ноги {money(alert.volume24h_weak_usd)}"
    lines.append(f"📊 {volume} · живёт {duration(alert.lifetime_ms)}")

    blocked = {block.split(":", 1)[0] for block in alert.blocks}
    for code, words in BLOCK_WORDS:
        if code in blocked:
            lines.append(f"🔒 {words}")
            break

    buttons = tuple(
        (f"{_venue(leg.exchange)} ↗", leg.url) for leg in (alert.long, alert.short) if leg.url
    )
    return Message(text="\n".join(lines), buttons=buttons)


def closed_note(lifetime_ms: int | None, peak_total_pct: str | None, reason: str) -> str:
    """Appended to the original message when the gap is gone, so the chat keeps the outcome."""
    words = {
        "converged": "цены сошлись",
        "timeout": "перестала считаться",
        "evicted": "вытеснена другими парами",
        "app_stop": "терминал остановлен",
        "gone": "ушла из ленты",
    }
    peak = f" · максимум был {percent(peak_total_pct)}" if peak_total_pct else ""
    return f"✓ прожила {duration(lifetime_ms)}{peak} · {words.get(reason, reason)}"


@dataclass(frozen=True, slots=True)
class DigestNumbers:
    day: str
    gaps: int
    best_token: str | None
    best_total_pct: str | None
    best_lifetime_ms: int | None
    median_lifetime_ms: int | None
    converged: int
    by_pair: tuple[tuple[str, int], ...]
    database_size: str | None = None
    uptime_ms: int | None = None


def digest(numbers: DigestNumbers) -> Message:
    lines = [f"📊 <b>Сутки · {html.escape(numbers.day)}</b>", ""]
    lines.append(f"вилок выше порога — {numbers.gaps}")
    if numbers.best_token:
        best = f"лучшая — {html.escape(numbers.best_token)} {percent(numbers.best_total_pct)}"
        if numbers.best_lifetime_ms:
            best += f" ({duration(numbers.best_lifetime_ms)})"
        lines.append(best)
    if numbers.median_lifetime_ms is not None:
        lines.append(f"медиана жизни — {duration(numbers.median_lifetime_ms)}")
    lines.append(f"сошлись сами — {numbers.converged} из {numbers.gaps}")
    if numbers.by_pair:
        lines.append("")
        lines.append("по парам бирж")
        for pair, count in numbers.by_pair:
            lines.append(f"  {html.escape(pair)} — {count}")
    tail = []
    if numbers.database_size:
        tail.append(f"база {html.escape(numbers.database_size)}")
    if numbers.uptime_ms:
        tail.append(f"терминал работает {duration(numbers.uptime_ms)}")
    if tail:
        lines.append("")
        lines.append(" · ".join(tail))
    return Message(text="\n".join(lines))


def alarm(kind: str, detail: str) -> Message:
    """Something the owner must know about a terminal they cannot see."""
    words = {
        "exchange_down": "нет связи с биржей",
        "exchange_up": "связь с биржей восстановлена",
        "db_unavailable": "база недоступна, записи копятся в буфере",
        "db_connected": "база снова на связи",
        "leg_lost": "нога пары осталась открытой",
        "liquidation_near": "позиция близко к ликвидации",
        "started": "терминал запущен",
        "stopped": "терминал остановлен",
    }
    icon = "🔴" if kind in ("exchange_down", "db_unavailable", "leg_lost", "liquidation_near") else "🟢"
    return Message(text=f"{icon} <b>{words.get(kind, kind)}</b>\n{html.escape(detail)}" if detail else f"{icon} <b>{words.get(kind, kind)}</b>")
