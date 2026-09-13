import asyncio
import logging
import httpx

import rankings

# Минимальный 24h объём (USD) на уровне скана — нижний пол.
# Пользовательский фильтр в parser.py применяет более высокий порог по выбору юзера.
MIN_VOLUME_USD = 50_000

log = logging.getLogger(__name__)

EXCHANGES = {
    "binance": "Binance",
    "bybit":   "Bybit",
    "mexc":    "MEXC",
}


def exchange_url(exchange: str, symbol: str) -> str:
    s = symbol.upper()
    urls = {
        "binance": f"https://www.binance.com/en/futures/{s}USDT",
        "bybit":   f"https://www.bybit.com/trade/usdt/{s}USDT",
        "mexc":    f"https://futures.mexc.com/exchange/{s}_USDT",
    }
    return urls.get(exchange, "")


def price_str(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:.4f}"
    else:
        return f"${p:.6f}"


def volume_str(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def normalize(raw: str) -> str | None:
    s = raw.upper().strip()
    for suffix in ["_USDT", "USDT"]:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if not s:
        return None
    if s.endswith("USD") or s[0].isdigit():
        return None
    return s


# ─── EXCHANGE FETCHERS ─────────────────────────────────────────────────────────
# Каждый возвращает: {symbol: {"last": float, "mark": float, "volume": float}}

async def fetch_binance(client: httpx.AsyncClient) -> dict[str, dict]:
    ticker_r, premium_r = await asyncio.gather(
        client.get("https://fapi.binance.com/fapi/v1/ticker/24hr"),
        client.get("https://fapi.binance.com/fapi/v1/premiumIndex"),
    )
    ticker_r.raise_for_status()
    premium_r.raise_for_status()

    marks: dict[str, float] = {}
    for item in premium_r.json():
        sym = normalize(item["symbol"])
        if not sym:
            continue
        mark = float(item.get("markPrice") or 0)
        if mark > 0:
            marks[sym] = mark

    out: dict[str, dict] = {}
    for item in ticker_r.json():
        sym = normalize(item["symbol"])
        if not sym or sym not in marks:
            continue
        last = float(item.get("lastPrice") or 0)
        vol  = float(item.get("quoteVolume") or 0)
        if last <= 0:
            continue
        out[sym] = {"last": last, "mark": marks[sym], "volume": vol}
    return out


async def fetch_bybit(client: httpx.AsyncClient) -> dict[str, dict]:
    r = await client.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear"},
    )
    r.raise_for_status()
    out: dict[str, dict] = {}
    for item in r.json()["result"]["list"]:
        if not item["symbol"].endswith("USDT"):
            continue
        sym = normalize(item["symbol"])
        if not sym:
            continue
        last = float(item.get("lastPrice") or 0)
        mark = float(item.get("markPrice") or 0)
        vol  = float(item.get("turnover24h") or 0)
        if last <= 0 or mark <= 0:
            continue
        out[sym] = {"last": last, "mark": mark, "volume": vol}
    return out


async def fetch_mexc(client: httpx.AsyncClient) -> dict[str, dict]:
    r = await client.get("https://contract.mexc.com/api/v1/contract/ticker")
    r.raise_for_status()
    out: dict[str, dict] = {}
    for item in r.json().get("data", []):
        if not item["symbol"].endswith("_USDT"):
            continue
        sym = normalize(item["symbol"])
        if not sym:
            continue
        last = float(item.get("lastPrice") or 0)
        mark = float(item.get("fairPrice") or 0)
        vol  = float(item.get("amount24") or 0)
        if last <= 0 or mark <= 0:
            continue
        out[sym] = {"last": last, "mark": mark, "volume": vol}
    return out


FETCHERS = {
    "binance": fetch_binance,
    "bybit":   fetch_bybit,
    "mexc":    fetch_mexc,
}


async def _fetch_with_retry(name: str, fn, client: httpx.AsyncClient, retries: int = 2):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return name, await fn(client)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
    return name, last_exc


async def fetch_all_marks() -> dict[str, dict[str, dict]]:
    """Returns {exchange: {symbol: {last, mark, volume}}}."""
    async with httpx.AsyncClient(timeout=20) as client:
        results = await asyncio.gather(
            *[_fetch_with_retry(name, fn, client) for name, fn in FETCHERS.items()]
        )
    out: dict[str, dict[str, dict]] = {}
    for exchange, result in results:
        if isinstance(result, Exception):
            log.warning(f"[mark_gap] {exchange}: {result}")
        else:
            out[exchange] = result
            log.info(f"[mark_gap] {exchange}: {len(result)} symbols")
    return out


def find_mark_gaps(
    data: dict[str, dict[str, dict]],
    min_pct: float = 3.0,
    max_pct: float = 50.0,
    min_volume: float = MIN_VOLUME_USD,
) -> list[dict]:
    results: list[dict] = []
    for exchange, ex_data in data.items():
        for symbol, d in ex_data.items():
            last = d["last"]
            mark = d["mark"]
            vol  = d["volume"]
            if last <= 0 or mark <= 0:
                continue
            if vol < min_volume:
                continue
            diff_pct = (mark - last) / last * 100  # знак: +премия / −дисконт
            abs_pct  = abs(diff_pct)
            if not (min_pct <= abs_pct <= max_pct):
                continue
            results.append({
                "symbol":   symbol,
                "exchange": exchange,
                "last":     last,
                "mark":     mark,
                "volume":   vol,
                "diff_pct": round(diff_pct, 2),
            })
    results.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)
    return results


async def scan_mark_gaps(min_pct: float = 3.0, max_pct: float = 50.0) -> list[dict] | None:
    await rankings.ensure_fresh()
    data = await fetch_all_marks()
    if not data:
        log.error("[mark_gap] Ни одна биржа не ответила")
        return None
    gaps = find_mark_gaps(data, min_pct, max_pct)
    log.info(f"[mark_gap] Найдено {len(gaps)} сигналов ({min_pct}%–{max_pct}%)")
    return gaps


async def scan_mark_gaps_confirmed(min_pct: float = 3.0, max_pct: float = 50.0) -> list[dict] | None:
    """Двойной скан с паузой 5с — возвращаем только сигналы, подтверждённые в обоих
    (берём меньший по модулю diff и сохраняем направление)."""
    await rankings.ensure_fresh()

    data1 = await fetch_all_marks()
    if not data1:
        log.error("[mark_gap] Скан 1: ни одна биржа не ответила")
        return None
    gaps1 = find_mark_gaps(data1, min_pct, max_pct)
    log.info(f"[mark_gap] Скан 1: {len(gaps1)} сигналов")
    if not gaps1:
        return []

    await asyncio.sleep(5)

    data2 = await fetch_all_marks()
    if not data2:
        log.warning("[mark_gap] Скан 2 не удался, возвращаем 1-й")
        return gaps1
    gaps2 = find_mark_gaps(data2, min_pct, max_pct)
    log.info(f"[mark_gap] Скан 2: {len(gaps2)} сигналов")

    gaps2_map = {(g["symbol"], g["exchange"]): g for g in gaps2}

    confirmed: list[dict] = []
    for g in gaps1:
        g2 = gaps2_map.get((g["symbol"], g["exchange"]))
        if g2 is None:
            log.info(f"[mark_gap] {g['symbol']}@{g['exchange']}: не подтверждён")
            continue
        if (g["diff_pct"] > 0) != (g2["diff_pct"] > 0):
            log.info(f"[mark_gap] {g['symbol']}@{g['exchange']}: направление сменилось")
            continue
        cg = dict(g)
        cg["diff_pct"] = round(
            g["diff_pct"] if abs(g["diff_pct"]) <= abs(g2["diff_pct"]) else g2["diff_pct"],
            2,
        )
        confirmed.append(cg)

    confirmed.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)
    log.info(f"[mark_gap] Подтверждено: {len(confirmed)} из {len(gaps1)}")
    return confirmed


# ─── FORMATTING ────────────────────────────────────────────────────────────────

def format_mark_gap(opp: dict, index: int) -> str:
    sym  = opp["symbol"]
    diff = opp["diff_pct"]
    ex   = opp["exchange"]

    display = EXCHANGES.get(ex, ex.upper())
    url     = exchange_url(ex, sym)
    link    = f'<a href="{url}">{display}</a>' if url else display

    grade = rankings.get_grade(sym)
    rank  = rankings.get_rank(sym)
    grade_str = f"{rankings.grade_emoji(grade)} Тир {grade}"
    rank_str  = f" (#{rank})" if rank else ""

    direction = "🔺 Премия (mark &gt; last)" if diff > 0 else "🔻 Дисконт (mark &lt; last)"
    sign = "+" if diff > 0 else ""

    lines = [
        f"<b>#{index} {sym}</b>  |  Δ <b>{sign}{diff:.2f}%</b>  |  {grade_str}{rank_str}",
        "",
        f"📍 Биржа: {link}",
        f"💲 Last:  {price_str(opp['last'])}",
        f"📊 Mark:  {price_str(opp['mark'])}",
        f"💧 Vol 24h: {volume_str(opp['volume'])}",
        "",
        direction,
        "\n<i>by @crypto_ludiki</i>",
    ]
    return "\n".join(lines)
