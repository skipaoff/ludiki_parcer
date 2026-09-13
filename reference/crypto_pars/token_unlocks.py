"""Upcoming token-unlock scanner backed by CryptoRank's public website API."""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

import funding as fd
import rankings

log = logging.getLogger(__name__)

CRYPTORANK_UNLOCK_URL = os.getenv(
    "CRYPTORANK_UNLOCK_URL",
    "https://api.cryptorank.io/v0/app/consolidated-vesting",
)
CRYPTORANK_PAGE_SIZE = 20
CRYPTORANK_MAX_PAGES = 20
SOURCE_MIN_DILUTION_PCT = 1.0
DEFAULT_HORIZON_HOURS = 24
DEFAULT_MIN_FUTURES_EXCHANGES = 3
UNLOCK_DILUTION_OPTIONS = (3.0, 5.0, 7.0, 10.0)


def normalize_dilution_threshold(value: float) -> float:
    """Map legacy values onto the unlock threshold scale."""
    for option in UNLOCK_DILUTION_OPTIONS:
        if value <= option:
            return option
    return UNLOCK_DILUTION_OPTIONS[-1]


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _estimate_total_supply(row: dict) -> float | None:
    estimates = []
    for tokens_key, percent_key in (
        ("unlockedTokens", "unlockedTokensPercent"),
        ("lockedTokens", "lockedTokensPercent"),
    ):
        tokens = _as_float(row.get(tokens_key))
        percent = _as_float(row.get(percent_key))
        if tokens is not None and tokens > 0 and percent is not None and percent > 0:
            estimates.append(tokens * 100 / percent)
    if estimates:
        return max(estimates)

    unlocked = _as_float(row.get("unlockedTokens")) or 0
    locked = _as_float(row.get("lockedTokens")) or 0
    combined = unlocked + locked
    return combined if combined > 0 else None


def normalize_unlock(
    row: dict,
    *,
    now: datetime,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> dict | None:
    """Convert one public CryptoRank row into the bot's normalized event."""
    if row.get("isHidden") or row.get("isAuthProtected"):
        return None

    event_at = _parse_date(row.get("date"))
    if event_at is None:
        return None
    now_utc = now.astimezone(timezone.utc)
    if not now_utc < event_at <= now_utc + timedelta(hours=horizon_hours):
        return None

    raw_symbol = row.get("symbol")
    symbol = str(raw_symbol).upper().strip() if raw_symbol else ""
    key = str(row.get("key") or "").strip()
    if not symbol or not key:
        return None

    allocations: dict[str, float] = {}
    unlock_tokens = 0.0
    for batch in row.get("nextUnlocks") or []:
        if not isinstance(batch, dict):
            continue
        batch_date = _parse_date(batch.get("date"))
        if batch_date is not None and batch_date != event_at:
            continue
        tokens = _as_float(batch.get("tokens"))
        if tokens is None or tokens <= 0:
            continue
        unlock_tokens += tokens
        name = str(batch.get("allocationName") or "Другие").strip()
        allocations[name] = allocations.get(name, 0.0) + tokens

    circulating_supply = _as_float(row.get("circulatingSupply"))
    if unlock_tokens <= 0 or circulating_supply is None or circulating_supply <= 0:
        return None

    dilution_pct = unlock_tokens / circulating_supply * 100
    total_supply = _estimate_total_supply(row)
    total_supply_pct = (
        unlock_tokens / total_supply * 100
        if total_supply is not None and total_supply > 0
        else None
    )
    price = _as_float(row.get("price"))

    return {
        "event_id": f"{key}:{event_at.isoformat()}",
        "symbol": symbol,
        "name": str(row.get("name") or symbol).strip(),
        "key": key,
        "date": event_at,
        "unlock_tokens": unlock_tokens,
        "circulating_supply": circulating_supply,
        "dilution_pct": dilution_pct,
        "total_supply": total_supply,
        "total_supply_pct": total_supply_pct,
        "price": price,
        "unlock_value_usd": unlock_tokens * price if price is not None else None,
        "market_cap": _as_float(row.get("marketCap")),
        "allocations": [
            {"name": name, "tokens": tokens}
            for name, tokens in sorted(
                allocations.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ],
        "source_url": f"https://cryptorank.io/price/{key}/vesting",
    }


async def _fetch_page(
    client: httpx.AsyncClient,
    *,
    skip: int,
    source_min_dilution_pct: float,
    retries: int = 1,
) -> dict:
    params = {
        "limit": CRYPTORANK_PAGE_SIZE,
        "skip": skip,
        "sortingColumn": "date",
        "sortingDirection": "ASC",
        "enableSmallUnlocks": "true",
        "nextUnlock[from]": source_min_dilution_pct,
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.get(CRYPTORANK_UNLOCK_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Unexpected CryptoRank response schema")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(2**attempt)
    raise last_error or RuntimeError("CryptoRank request failed")


async def fetch_upcoming_unlocks(
    *,
    now: datetime | None = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    source_min_dilution_pct: float = SOURCE_MIN_DILUTION_PCT,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetch all public unlocks that happen in the next ``horizon_hours``."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end_at = now_utc + timedelta(hours=horizon_hours)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": "crypto-pars/2.0"},
        )

    events: dict[str, dict] = {}
    try:
        assert client is not None
        for page in range(CRYPTORANK_MAX_PAGES):
            payload = await _fetch_page(
                client,
                skip=page * CRYPTORANK_PAGE_SIZE,
                source_min_dilution_pct=source_min_dilution_pct,
            )
            rows = payload["data"]
            visible_dates = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_date = _parse_date(row.get("date"))
                if row_date is not None:
                    visible_dates.append(row_date)
                event = normalize_unlock(
                    row,
                    now=now_utc,
                    horizon_hours=horizon_hours,
                )
                if event is not None:
                    events[event["event_id"]] = event

            if len(rows) < CRYPTORANK_PAGE_SIZE:
                break
            if visible_dates and max(visible_dates) > end_at:
                break
        else:
            log.warning(
                "[unlocks] Reached CryptoRank pagination safety limit (%s pages)",
                CRYPTORANK_MAX_PAGES,
            )
    finally:
        if own_client and client is not None:
            await client.aclose()

    result = sorted(
        events.values(),
        key=lambda event: (event["date"], -event["dilution_pct"], event["symbol"]),
    )
    log.info("[unlocks] CryptoRank returned %s events in the next %sh", len(result), horizon_hours)
    return result


def qualify_by_futures(
    events: list[dict],
    futures_universe: dict[str, set[str]],
    *,
    min_exchanges: int = DEFAULT_MIN_FUTURES_EXCHANGES,
) -> list[dict]:
    """Keep events whose symbol has futures on enough independent exchanges."""
    qualified = []
    for event in events:
        symbol = event["symbol"]
        exchanges = sorted(
            exchange
            for exchange, symbols in futures_universe.items()
            if symbol in symbols
        )
        if len(exchanges) < min_exchanges:
            continue
        qualified.append({**event, "futures_exchanges": exchanges})
    return qualified


async def scan_upcoming_unlocks(
    *,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    min_futures_exchanges: int = DEFAULT_MIN_FUTURES_EXCHANGES,
    allowed_exchanges: set[str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    events = await fetch_upcoming_unlocks(now=now, horizon_hours=horizon_hours)
    if not events:
        return []

    futures_universe = await fd.get_futures_universe(
        allowed_exchanges=allowed_exchanges,
        max_age_seconds=3600,
    )
    qualified = qualify_by_futures(
        events,
        futures_universe,
        min_exchanges=min_futures_exchanges,
    )
    await rankings.ensure_fresh()
    log.info(
        "[unlocks] %s/%s events trade on at least %s futures exchanges",
        len(qualified),
        len(events),
        min_futures_exchanges,
    )
    return qualified


def filter_unlocks(events: list[dict], min_dilution_pct: float) -> list[dict]:
    return [
        event
        for event in events
        if event["dilution_pct"] >= min_dilution_pct
    ]


def _format_number(value: float | None, *, prefix: str = "") -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    for threshold, suffix in (
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if absolute >= threshold:
            return f"{prefix}{value / threshold:.2f}{suffix}"
    return f"{prefix}{value:,.2f}"


def _exchange_link(exchange: str, symbol: str) -> str:
    label = html.escape(fd.EXCHANGES.get(exchange, exchange.upper()))
    url = html.escape(fd.exchange_url(exchange, symbol), quote=True)
    return f'<a href="{url}">{label}</a>' if url else label


def _remaining_label(event_at: datetime, now: datetime) -> str:
    seconds = max(0, int((event_at - now.astimezone(timezone.utc)).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def format_unlock(event: dict, index: int, *, now: datetime | None = None) -> str:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_symbol = event["symbol"]
    symbol = html.escape(raw_symbol)
    event_at = event["date"].astimezone(timezone.utc)
    rank = rankings.get_rank(raw_symbol)
    grade = rankings.get_grade(raw_symbol)
    rank_label = f"#{rank}" if rank is not None else "без ранга"
    grade_label = f"{rankings.grade_emoji(grade)} Тир {grade} ({rank_label})"
    source_url = html.escape(event["source_url"], quote=True)
    symbol_link = f'<a href="{source_url}">{symbol}</a>'
    unlock_value = event.get("unlock_value_usd")
    unlock_value_label = (
        f"~{_format_number(unlock_value, prefix='$')}"
        if unlock_value is not None
        else "—"
    )
    market_cap_label = _format_number(event.get("market_cap"), prefix="$")

    lines = [
        (
            f"<b>#{index} {symbol_link}</b> | Разлок через "
            f"<b>{_remaining_label(event_at, now_utc)}</b> | {grade_label}"
        ),
        "",
        f"📅 <b>{event_at.strftime('%d.%m.%Y %H:%M')} UTC</b>",
        (
            f"🔓 Разлок: <b>{_format_number(event['unlock_tokens'])} / "
            f"{_format_number(event.get('total_supply'))} {symbol}</b>"
        ),
        (
            f"💵 Стоимость разлока: <b>{unlock_value_label} / "
            f"{market_cap_label}</b>"
        ),
        (
            "📈 Рост circulating supply: "
            f"<b>+{event['dilution_pct']:.2f}%</b>"
        ),
    ]
    if event.get("total_supply_pct") is not None:
        lines.append(
            f"🧮 От общей эмиссии: <b>{event['total_supply_pct']:.2f}%</b>"
        )
    lines.extend(
        [
            "",
            (
                f"📊 Фьючерсы: <b>{len(event['futures_exchanges'])} бирж</b>\n"
                + " • ".join(
                    _exchange_link(exchange, raw_symbol)
                    for exchange in event["futures_exchanges"]
                )
            ),
        ]
    )

    allocations = event.get("allocations") or []
    if allocations:
        lines.extend(["", "🗂 <b>Кому разблокируют:</b>"])
        for allocation in allocations[:3]:
            allocation_name = html.escape(allocation["name"])
            lines.append(
                f"  • {allocation_name}: "
                f"{_format_number(allocation['tokens'])} {symbol}"
            )

    lines.extend(
        [
            "",
            "<i>by @crypto_ludiki</i>",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_HORIZON_HOURS",
    "DEFAULT_MIN_FUTURES_EXCHANGES",
    "UNLOCK_DILUTION_OPTIONS",
    "fetch_upcoming_unlocks",
    "filter_unlocks",
    "format_unlock",
    "normalize_dilution_threshold",
    "normalize_unlock",
    "qualify_by_futures",
    "scan_upcoming_unlocks",
]
