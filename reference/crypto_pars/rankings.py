import asyncio
import logging
import httpx
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

CACHE_TTL_HOURS = 6
PAGES           = 6      # 6 × 250 = top 1500 coins
PER_PAGE        = 250

# symbol.upper() → rank (int)
_rank_cache: dict[str, int] = {}
_cache_updated_at: datetime | None = None


async def _fetch_page(client: httpx.AsyncClient, page: int) -> list[dict]:
    for attempt in range(3):
        response = await client.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order":       "market_cap_rank",
                "per_page":    PER_PAGE,
                "page":        page,
                "sparkline":   "false",
            },
        )
        if response.status_code != 429 or attempt == 2:
            response.raise_for_status()
            return response.json()

        retry_after = response.headers.get("Retry-After")
        try:
            requested_delay = float(retry_after) if retry_after else 2**attempt
            delay = min(max(requested_delay, 1.0), 3.0)
        except ValueError:
            delay = 2**attempt
        log.warning(
            "[rankings] CoinGecko rate limit on page %s, retrying in %.1fs",
            page,
            delay,
        )
        await asyncio.sleep(delay)

    raise RuntimeError(f"CoinGecko page {page} exhausted retries")


async def refresh_rankings() -> bool:
    global _rank_cache, _cache_updated_at
    log.info("[rankings] Fetching CoinGecko top rankings...")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            pages = await asyncio.gather(
                *[_fetch_page(client, page) for page in range(1, PAGES + 1)],
                return_exceptions=True,
            )

        new_cache: dict[str, int] = {}
        failed_pages = 0
        for page_data in pages:
            if isinstance(page_data, Exception):
                log.warning(f"[rankings] Page error: {page_data}")
                failed_pages += 1
                continue
            for coin in page_data:
                sym  = coin.get("symbol", "").upper().strip()
                rank = coin.get("market_cap_rank")
                if sym and rank:
                    # Keep the best rank if symbol appears twice (e.g. USDT)
                    if sym not in new_cache or rank < new_cache[sym]:
                        new_cache[sym] = rank

        if failed_pages and _rank_cache:
            merged_cache = dict(_rank_cache)
            merged_cache.update(new_cache)
            _rank_cache = merged_cache
            _cache_updated_at = (
                datetime.now()
                - timedelta(hours=CACHE_TTL_HOURS)
                + timedelta(minutes=5)
            )
            log.warning(
                "[rankings] Merged partial refresh into cache; %s page(s) failed, "
                "will retry in 5 minutes",
                failed_pages,
            )
            return False
        if not new_cache:
            log.error("[rankings] No rankings received")
            return False

        _rank_cache = new_cache
        _cache_updated_at = (
            datetime.now()
            if not failed_pages
            else datetime.now() - timedelta(hours=CACHE_TTL_HOURS) + timedelta(minutes=5)
        )
        log.info(
            "[rankings] Loaded %s symbols%s",
            len(_rank_cache),
            f" ({failed_pages} page(s) missing)" if failed_pages else "",
        )
        return True

    except Exception as e:
        log.error(f"[rankings] Failed to refresh: {e}")
        return False


async def ensure_fresh():
    """Refresh cache if it's stale or empty."""
    global _cache_updated_at
    if (
        not _rank_cache or
        _cache_updated_at is None or
        datetime.now() - _cache_updated_at > timedelta(hours=CACHE_TTL_HOURS)
    ):
        await refresh_rankings()


def get_rank(symbol: str) -> int | None:
    return _rank_cache.get(symbol.upper())


def get_grade(symbol: str) -> str:
    """Return A / B / C / ? based on CoinGecko market cap rank."""
    rank = get_rank(symbol)
    if rank is None:
        return "?"
    if rank <= 500:
        return "A"
    if rank <= 1000:
        return "B"
    return "C"


def grade_emoji(grade: str) -> str:
    return {"A": "🟢", "B": "🟡", "C": "🔴"}.get(grade, "⚪️")
