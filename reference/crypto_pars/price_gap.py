import asyncio
import logging
import time
import httpx
import rankings

# Minimum 24h liquidity (quote volume or OI×price) in USD to include a symbol.
# Populated by each fetcher; symbols with no data at all pass through.
MIN_LIQUIDITY_USD = 50_000  # $50k

# symbol → best liquidity figure seen across exchanges this scan (USD)
_liquidity_cache: dict[str, float] = {}

log = logging.getLogger(__name__)

# ─── DISPLAY & URL ─────────────────────────────────────────────────────────────

EXCHANGES = {
    "binance":     "Binance",
    "bybit":       "Bybit",
    "okx":         "OKX",
    "gate":        "Gate.io",
    "mexc":        "MEXC",
    "bitget":      "Bitget",
    "bingx":       "BingX",
    "hyperliquid": "Hyperliquid",
    "phemex":      "Phemex",
    "aster":       "Aster",
    "variational": "Variational",
}


def exchange_url(exchange: str, symbol: str) -> str:
    s  = symbol.upper()
    sl = symbol.lower()
    urls = {
        "binance":     f"https://www.binance.com/en/futures/{s}USDT",
        "bybit":       f"https://www.bybit.com/trade/usdt/{s}USDT",
        "okx":         f"https://www.okx.com/trade-swap/{sl}-usdt-swap",
        "gate":        f"https://www.gate.com/futures/USDT/{s}_USDT",
        "mexc":        f"https://futures.mexc.com/exchange/{s}_USDT",
        "bitget":      f"https://www.bitget.com/futures/usdt/{s}USDT",
        "bingx":       f"https://bingx.com/en/perpetual/{s}-USDT/",
        "hyperliquid": f"https://app.hyperliquid.xyz/trade/{s}",
        "phemex":      f"https://phemex.com/trade/{s}USDT",
        "aster":       f"https://www.asterdex.com/en/trade/pro/futures/{s}USDT",
        "variational": f"https://omni.variational.io/perpetual/{s}",
    }
    return urls.get(exchange, "")


def price_str(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:.4f}"
    else:
        return f"${p:.6f}"


def _update_liq(sym: str, usd: float) -> None:
    """Update liquidity cache with the best (max) value seen for a symbol."""
    if usd > 0:
        _liquidity_cache[sym] = max(_liquidity_cache.get(sym, 0), usd)


# ─── SYMBOL NORMALIZATION ──────────────────────────────────────────────────────

def normalize(raw: str, exchange: str) -> str | None:
    s = raw.upper().strip()
    for suffix in ["-USDT-SWAP", "_USDT", "-USDT", "USDT"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    if not s:
        return None
    if s.endswith("USD") or s.startswith(".") or s.startswith("@"):
        return None
    if s and s[0].isdigit():
        return None
    if exchange == "hyperliquid" and s.startswith("K") and len(s) > 1:
        return None
    return s


# ─── EXCHANGE FETCHERS ─────────────────────────────────────────────────────────

async def fetch_binance(client: httpx.AsyncClient) -> dict[str, float]:
    now_ms = time.time() * 1000
    r = await client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    r.raise_for_status()
    out = {}
    for item in r.json():
        sym = normalize(item["symbol"], "binance")
        if not sym:
            continue
        close_ms = item.get("closeTime", 0)
        if close_ms and (now_ms - close_ms) > 48 * 3600 * 1000:
            continue
        vol = float(item.get("quoteVolume", 0))
        _update_liq(sym, vol)
        if vol >= MIN_LIQUIDITY_USD:
            out[sym] = float(item["lastPrice"])
    return out


async def fetch_bybit(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear"},
    )
    r.raise_for_status()
    out = {}
    for item in r.json()["result"]["list"]:
        sym = normalize(item["symbol"], "bybit")
        if not sym or not item.get("lastPrice"):
            continue
        _update_liq(sym, float(item.get("turnover24h", 0)))
        out[sym] = float(item["lastPrice"])
    return out


async def fetch_okx(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get(
        "https://www.okx.com/api/v5/market/tickers",
        params={"instType": "SWAP"},
    )
    r.raise_for_status()
    out = {}
    for item in r.json()["data"]:
        if not item["instId"].endswith("-USDT-SWAP"):
            continue
        sym = normalize(item["instId"], "okx")
        if not sym or not item.get("last"):
            continue
        _update_liq(sym, float(item.get("volCcy24h", 0)))
        out[sym] = float(item["last"])
    return out


async def fetch_gate(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get("https://api.gateio.ws/api/v4/futures/usdt/tickers")
    r.raise_for_status()
    out = {}
    for item in r.json():
        sym = normalize(item["contract"], "gate")
        if not sym or not item.get("last"):
            continue
        _update_liq(sym, float(item.get("volume_24h_settle", 0)))
        out[sym] = float(item["last"])
    return out


async def fetch_mexc(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get("https://contract.mexc.com/api/v1/contract/ticker")
    r.raise_for_status()
    out = {}
    for item in r.json().get("data", []):
        sym = normalize(item["symbol"], "mexc")
        if not sym or not item.get("lastPrice"):
            continue
        _update_liq(sym, float(item.get("amount24", 0)))
        out[sym] = float(item["lastPrice"])
    return out


async def fetch_bitget(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get(
        "https://api.bitget.com/api/v2/mix/market/tickers",
        params={"productType": "USDT-FUTURES"},
    )
    r.raise_for_status()
    out = {}
    for item in r.json().get("data", []):
        sym = normalize(item["symbol"], "bitget")
        if not sym or not item.get("lastPr"):
            continue
        _update_liq(sym, float(item.get("quoteVolume", 0)))
        out[sym] = float(item["lastPr"])
    return out


async def fetch_bingx(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker")
    r.raise_for_status()
    out = {}
    for item in r.json().get("data", []):
        sym = normalize(item["symbol"], "bingx")
        if not sym or not item.get("lastPrice"):
            continue
        _update_liq(sym, float(item.get("quoteVolume", 0)))
        out[sym] = float(item["lastPrice"])
    return out


async def fetch_phemex(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get(
        "https://api.phemex.com/md/v3/ticker/24hr/all",
        params={"type": "Perpetual"},
    )
    r.raise_for_status()
    out = {}
    for item in r.json().get("result", []):
        sym = normalize(item["symbol"], "phemex")
        if not sym or not item.get("lastRp"):
            continue
        # turnoverRp is in USD scaled ×10^8
        turnover_rp = float(item.get("turnoverRp", 0))
        if turnover_rp:
            _update_liq(sym, turnover_rp / 1e8)
        out[sym] = float(item["lastRp"])
    return out


ASTER_MIN_OI_USD = 100_000  # $100k минимальный открытый интерес для Aster


async def _fetch_aster_oi(client: httpx.AsyncClient, raw_symbol: str) -> tuple[str, float]:
    """Возвращает (raw_symbol, oi_usd). При ошибке — 0.0."""
    try:
        r = await client.get(
            "https://fapi.asterdex.com/fapi/v1/openInterest",
            params={"symbol": raw_symbol},
        )
        r.raise_for_status()
        oi = float(r.json().get("openInterest", 0))
        return raw_symbol, oi
    except Exception:
        return raw_symbol, 0.0


async def fetch_aster(client: httpx.AsyncClient) -> dict[str, float]:
    now_ms = time.time() * 1000
    r = await client.get("https://fapi.asterdex.com/fapi/v1/ticker/24hr")
    r.raise_for_status()

    # Шаг 1: фильтр по объёму и времени — оставляем кандидатов
    candidates: dict[str, tuple[str, float]] = {}  # sym → (raw_symbol, price)
    for item in r.json():
        sym = normalize(item["symbol"], "aster")
        if not sym:
            continue
        close_ms = item.get("closeTime", 0)
        if close_ms and (now_ms - close_ms) > 48 * 3600 * 1000:
            continue
        vol = float(item.get("quoteVolume", 0))
        _update_liq(sym, vol)
        if vol >= MIN_LIQUIDITY_USD:
            candidates[sym] = (item["symbol"], float(item["lastPrice"]))

    if not candidates:
        return {}

    # Шаг 2: параллельно запрашиваем OI только для кандидатов
    oi_results = await asyncio.gather(
        *[_fetch_aster_oi(client, raw_sym) for _, (raw_sym, _) in candidates.items()]
    )
    oi_map: dict[str, float] = dict(oi_results)  # raw_symbol → oi (в базовом активе)

    # Шаг 3: фильтр по OI > $100k
    out = {}
    for sym, (raw_sym, price) in candidates.items():
        oi_base = oi_map.get(raw_sym, 0.0)
        oi_usd  = oi_base * price
        if oi_usd > 0 and oi_usd < ASTER_MIN_OI_USD:
            log.debug(f"[price_gap] aster/{sym}: OI ${oi_usd:,.0f} < ${ASTER_MIN_OI_USD:,}, пропуск")
            continue
        out[sym] = price
    return out


async def fetch_variational(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get(
        "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
    )
    r.raise_for_status()
    out = {}
    for item in r.json().get("listings", []):
        sym = item.get("ticker", "").upper().strip()
        price = item.get("mark_price")
        if sym and price:
            out[sym] = float(price)
    return out


async def fetch_hyperliquid(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
    )
    r.raise_for_status()
    meta, ctxs = r.json()
    out = {}
    for asset, ctx in zip(meta["universe"], ctxs):
        sym_norm = normalize(asset["name"], "hyperliquid")
        if not sym_norm:
            continue
        if ctx.get("midPx") is None:
            continue
        oi = float(ctx.get("openInterest", 0))
        if oi == 0:
            continue
        price = float(ctx["midPx"])
        # Prefer daily notional volume; fall back to OI×price
        ntl_vol = float(ctx.get("dayNtlVlm", 0))
        _update_liq(sym_norm, ntl_vol if ntl_vol else oi * price)
        out[sym_norm] = price
    return out


# ─── SCAN ──────────────────────────────────────────────────────────────────────

FETCHERS = {
    "binance":     fetch_binance,
    "bybit":       fetch_bybit,
    "okx":         fetch_okx,
    "gate":        fetch_gate,
    "mexc":        fetch_mexc,
    "bitget":      fetch_bitget,
    "bingx":       fetch_bingx,
    "hyperliquid": fetch_hyperliquid,
    "phemex":      fetch_phemex,
    "aster":       fetch_aster,
    "variational": fetch_variational,
}


async def _fetch_with_retry(name: str, fn, client: httpx.AsyncClient, retries: int = 2) -> tuple[str, dict | Exception]:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            result = await fn(client)
            return name, result
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
    return name, last_exc


async def fetch_all_prices() -> dict[str, dict[str, float]]:
    """Fetch prices from all exchanges in parallel with retry. Returns {exchange: {symbol: price}}."""
    _liquidity_cache.clear()
    async with httpx.AsyncClient(timeout=20) as client:
        results = await asyncio.gather(
            *[_fetch_with_retry(name, fn, client) for name, fn in FETCHERS.items()],
        )
    prices = {}
    for exchange, result in results:
        if isinstance(result, Exception):
            log.warning(f"[price_gap] {exchange}: {result}")
        else:
            prices[exchange] = result
            log.info(f"[price_gap] {exchange}: {len(result)} symbols")
    return prices


def find_gaps(
    prices: dict[str, dict[str, float]],
    min_gap: float = 3.0,
    max_gap: float = 50.0,
) -> list[dict]:
    all_symbols: set[str] = set()
    for ex_prices in prices.values():
        all_symbols.update(ex_prices.keys())

    results = []
    for symbol in all_symbols:
        sym_prices = {
            ex: p
            for ex, ex_prices in prices.items()
            if (p := ex_prices.get(symbol)) is not None and p > 0
        }
        if len(sym_prices) < 3:
            continue

        # Liquidity filter: skip if we have data and it's below threshold
        liq = _liquidity_cache.get(symbol, 0)
        if 0 < liq < MIN_LIQUIDITY_USD:
            continue

        min_price = min(sym_prices.values())
        max_price = max(sym_prices.values())
        if min_price <= 0:
            continue

        gap_pct = (max_price - min_price) / min_price * 100
        if not (min_gap <= gap_pct <= max_gap):
            continue

        long_ex  = min(sym_prices, key=lambda e: sym_prices[e])
        short_ex = max(sym_prices, key=lambda e: sym_prices[e])

        results.append({
            "symbol":         symbol,
            "gap_pct":        round(gap_pct, 2),
            "long_exchange":  long_ex,
            "long_price":     sym_prices[long_ex],
            "short_exchange": short_ex,
            "short_price":    sym_prices[short_ex],
            "all_prices":     dict(sorted(sym_prices.items(), key=lambda x: x[1])),
        })

    results.sort(key=lambda x: x["gap_pct"], reverse=True)
    return results


async def scan_price_gaps(min_gap: float = 3.0, max_gap: float = 50.0) -> list[dict] | None:
    await rankings.ensure_fresh()
    prices = await fetch_all_prices()
    if len(prices) < 2:
        log.error("[price_gap] Not enough exchanges responded")
        return None
    gaps = find_gaps(prices, min_gap, max_gap)
    log.info(f"[price_gap] Found {len(gaps)} gaps ({min_gap}%–{max_gap}%)")
    return gaps


async def scan_price_gaps_confirmed(min_gap: float = 3.0, max_gap: float = 50.0) -> list[dict] | None:
    """
    Двойная проверка: два скана с паузой 5 сек.
    Возвращает только гепы, подтверждённые в обоих сканах (берём меньший из двух).
    """
    await rankings.ensure_fresh()

    # Скан 1
    prices1 = await fetch_all_prices()
    if len(prices1) < 2:
        log.error("[price_gap] Скан 1: мало бирж ответило")
        return None
    gaps1 = find_gaps(prices1, min_gap, max_gap)
    log.info(f"[price_gap] Скан 1: {len(gaps1)} гепов ({min_gap}%–{max_gap}%)")
    if not gaps1:
        return []

    await asyncio.sleep(5)

    # Скан 2
    prices2 = await fetch_all_prices()
    if len(prices2) < 2:
        log.warning("[price_gap] Скан 2 не удался, возвращаем результат скана 1")
        return gaps1
    gaps2 = find_gaps(prices2, min_gap, max_gap)
    log.info(f"[price_gap] Скан 2: {len(gaps2)} гепов")

    # Индекс второго скана по (symbol, long_exchange, short_exchange)
    gaps2_map = {
        (g["symbol"], g["long_exchange"], g["short_exchange"]): g
        for g in gaps2
    }

    confirmed = []
    for g in gaps1:
        key = (g["symbol"], g["long_exchange"], g["short_exchange"])
        g2 = gaps2_map.get(key)
        if g2 is None:
            log.info(
                f"[price_gap] {g['symbol']} ({g['long_exchange']}→{g['short_exchange']}): "
                f"не подтверждён (геп исчез)"
            )
            continue
        confirmed_g = dict(g)
        confirmed_g["gap_pct"] = round(min(g["gap_pct"], g2["gap_pct"]), 2)
        confirmed.append(confirmed_g)

    confirmed.sort(key=lambda x: x["gap_pct"], reverse=True)
    log.info(f"[price_gap] Подтверждено: {len(confirmed)} из {len(gaps1)}")
    return confirmed


# ─── FORMATTING ────────────────────────────────────────────────────────────────

def format_gap(opp: dict, index: int) -> str:
    sym      = opp["symbol"]
    gap      = opp["gap_pct"]
    long_ex  = opp["long_exchange"]
    short_ex = opp["short_exchange"]

    def link(exchange: str) -> str:
        display = EXCHANGES.get(exchange, exchange.upper())
        url     = exchange_url(exchange, sym)
        return f'<a href="{url}">{display}</a>' if url else display

    grade = rankings.get_grade(sym)
    rank  = rankings.get_rank(sym)
    grade_str = f"{rankings.grade_emoji(grade)} Тир {grade}"
    rank_str  = f" (#{rank})" if rank else ""

    lines = [
        f"<b>#{index} {sym}</b>  |  гэп <b>+{gap:.2f}%</b>  |  {grade_str}{rank_str}",
        "",
        f"🟢 <b>LONG</b>  → {link(long_ex)}  ({price_str(opp['long_price'])})",
        f"🔴 <b>SHORT</b> → {link(short_ex)}  ({price_str(opp['short_price'])})",
    ]

    others = {
        ex: p for ex, p in opp["all_prices"].items()
        if ex not in (long_ex, short_ex)
    }
    if others:
        lines.append("")
        lines.append("📊 Другие биржи:")
        for ex, p in others.items():
            lines.append(f"  • {link(ex)}: {price_str(p)}")

    lines.append("\n<i>by @crypto_ludiki</i>")
    return "\n".join(lines)
