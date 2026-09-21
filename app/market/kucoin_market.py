"""
VFP: KuCoin Futures market data into MarketState — best prices of every contract from REST, mark, index and volume from the contract list, order books of candidates from websocket snapshots.
Changes when: KuCoin changes its futures tickers, contract list or level2 channels.
Anti-goal:
1. A fixed websocket address — every connection needs a fresh token from POST /api/v1/bullet-public.
2. The tickerV2 channel for best prices — about 40 messages a second per symbol (checked live 15.09.2026).
3. Converting every book snapshot — level2Depth50 sends 50 levels about 8 times a second; books go through a BookBuffer.
4. A silent socket — KuCoin expects a ping within its pingInterval (18 s).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.market.depth_pool import BookBuffer, DepthPool, Poller
from app.market.state import MarketState

BULLET_URL = "https://api-futures.kucoin.com/api/v1/bullet-public"
TICKERS_URL = "https://api-futures.kucoin.com/api/v1/allTickers"
CONTRACTS_URL = "https://api-futures.kucoin.com/api/v1/contracts/active"
CONTRACTS_POLL_S = 60
SYMBOLS_PER_CONNECTION = 50
TOPIC = "/contractMarket/level2Depth50"
EXCHANGE = "kucoin"
_ids = itertools.count(1)


def depth_messages(symbols: list[str], subscribe: bool) -> Iterable[str]:
    yield orjson.dumps(
        {"id": str(next(_ids)), "type": "subscribe" if subscribe else "unsubscribe", "topic": f"{TOPIC}:{','.join(symbols)}", "response": True}
    ).decode()


def handle_frame(buffer: BookBuffer, state: MarketState, raw: str | bytes) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    topic = message.get("topic") or ""
    if message.get("subject") == "level2" and topic.startswith(TOPIC):
        data = message.get("data") or {}
        state.count(EXCHANGE)
        buffer.put(topic.split(":", 1)[1], data.get("bids") or [], data.get("asks") or [], int(data.get("ts") or data.get("timestamp") or 0))


def ticker_rows(payload: dict[str, Any]) -> list[tuple[str, float, float]]:
    """GET /api/v1/allTickers — (symbol, bestBidPrice, bestAskPrice)."""
    if str(payload.get("code")) != "200000":
        raise RuntimeError(f"kucoin tickers answer {payload.get('code')}")
    return [
        (item["symbol"], float(item.get("bestBidPrice") or 0), float(item.get("bestAskPrice") or 0))
        for item in payload.get("data") or []
        if item.get("symbol")
    ]


def contract_rows(payload: dict[str, Any]) -> list[tuple[str, float | None, float | None, float | None]]:
    """GET /api/v1/contracts/active — (symbol, markPrice, indexPrice, turnoverOf24h)."""
    if str(payload.get("code")) != "200000":
        raise RuntimeError(f"kucoin contracts answer {payload.get('code')}")
    number = lambda item, key: float(item[key]) if item.get(key) else None  # noqa: E731
    return [
        (item["symbol"], number(item, "markPrice"), number(item, "indexPrice"), number(item, "turnoverOf24h"))
        for item in payload.get("data") or []
        if item.get("symbol")
    ]


class KucoinMarket:
    def __init__(self, state: MarketState, poll_ms: int) -> None:
        self._state = state
        self._buffer = BookBuffer(state, EXCHANGE, interval_s=0.2)
        self._session: aiohttp.ClientSession | None = None
        self._pool = DepthPool(
            EXCHANGE, self._socket_url, lambda raw: handle_frame(self._buffer, self._state, raw), depth_messages,
            per_connection=SYMBOLS_PER_CONNECTION, batch_size=1, send_interval_s=0.1,
            heartbeat=(15, orjson.dumps({"id": "ping", "type": "ping"}).decode()),
        )
        self._tickers = Poller("kucoin tickers", poll_ms / 1000, self._poll_tickers)
        self._contracts = Poller("kucoin contracts", CONTRACTS_POLL_S, self._poll_contracts)

    async def _socket_url(self) -> str:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(BULLET_URL) as response:
                response.raise_for_status()
                bullet = (orjson.loads(await response.read()))["data"]
        server = bullet["instanceServers"][0]
        return f"{server['endpoint']}?token={bullet['token']}&connectId=ludik{next(_ids)}"

    def set_depth(self, symbols: Iterable[str]) -> None:
        self._pool.set_depth(symbols)

    def resubscribe_depth(self, symbol: str) -> None:
        self._pool.resubscribe(symbol)

    async def _get(self, url: str) -> dict[str, Any]:
        assert self._session is not None
        async with self._session.get(url) as response:
            response.raise_for_status()
            return orjson.loads(await response.read())

    async def _poll_tickers(self) -> None:
        now = int(time.time() * 1000)
        for symbol, bid, ask in ticker_rows(await self._get(TICKERS_URL)):
            if bid > 0 and ask > 0:
                self._state.set_top(EXCHANGE, symbol, bid, ask, now)
        self._state.count(EXCHANGE)

    async def _poll_contracts(self) -> None:
        for symbol, mark, index, volume in contract_rows(await self._get(CONTRACTS_URL)):
            self._state.set_mark(EXCHANGE, symbol, mark, index, volume)

    async def run(self) -> None:
        self._pool.start()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                self._session = session
                await asyncio.gather(self._tickers.run(), self._contracts.run(), self._buffer.run())
        finally:
            self._pool.stop()

    def stats(self) -> dict[str, Any]:
        return {**self._pool.stats(), "polls": self._tickers.polls, "poll_errors": self._tickers.errors + self._contracts.errors}
