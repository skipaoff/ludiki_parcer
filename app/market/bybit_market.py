"""
VFP: Bybit linear market data into MarketState — best prices, mark, index and volume of every contract from REST, order books of candidates kept locally from websocket snapshots and deltas.
Changes when: Bybit changes its v5 tickers or order book channels.
Anti-goal:
1. A book built from deltas without its snapshot — deltas for a symbol are ignored until a snapshot has arrived.
2. Publishing every delta — orderbook.50 sends about 27 deltas a second per symbol; books are published through a
   BookBuffer (checked live 15.09.2026).
3. A silent socket — Bybit drops public connections without a ping every 20 seconds.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.system.tls import client_session
from app.market.depth_pool import BookBuffer, DepthPool, Poller
from app.market.state import MarketState

WS_URL = "wss://stream.bybit.com/v5/public/linear"
TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
SYMBOLS_PER_CONNECTION = 50
ARGS_PER_MESSAGE = 10
DEPTH = 50
EXCHANGE = "bybit"
PING = orjson.dumps({"op": "ping"}).decode()


def depth_topic(symbol: str) -> str:
    return f"orderbook.{DEPTH}.{symbol}"


def depth_messages(symbols: list[str], subscribe: bool) -> Iterable[str]:
    topics = [depth_topic(symbol) for symbol in symbols]
    if subscribe:
        # A repeated subscription is refused while the old one lives; dropping it first makes Bybit send a fresh snapshot.
        yield orjson.dumps({"op": "unsubscribe", "args": topics}).decode()
    yield orjson.dumps({"op": "subscribe" if subscribe else "unsubscribe", "args": topics}).decode()


class LocalBooks:
    """Bybit books rebuilt from a snapshot and the deltas after it; zero size removes a level."""

    def __init__(self) -> None:
        self.books: dict[str, tuple[dict[str, str], dict[str, str]]] = {}

    def apply(self, message: dict[str, Any]) -> str | None:
        data = message.get("data") or {}
        symbol = data.get("s")
        if not symbol:
            return None
        if message.get("type") == "snapshot":
            self.books[symbol] = (dict(data.get("b") or []), dict(data.get("a") or []))
            return symbol
        book = self.books.get(symbol)
        if book is None:
            return None
        for side, levels in ((book[0], data.get("b") or []), (book[1], data.get("a") or [])):
            for price, size in levels:
                if float(size) == 0:
                    side.pop(price, None)
                else:
                    side[price] = size
        return symbol

    def levels(self, symbol: str) -> tuple[list[list[str]], list[list[str]]]:
        bids, asks = self.books[symbol]
        return [[price, size] for price, size in bids.items()], [[price, size] for price, size in asks.items()]


def handle_frame(books: LocalBooks, buffer: BookBuffer, state: MarketState, raw: str | bytes) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    topic = message.get("topic") or ""
    if topic.startswith("orderbook."):
        symbol = books.apply(message)
        if symbol is not None:
            bids, asks = books.levels(symbol)
            buffer.put(symbol, bids, asks, int(message.get("cts") or message.get("ts") or 0))


def ticker_rows(payload: dict[str, Any]) -> list[tuple[str, float, float, float | None, float | None, float | None]]:
    """GET /v5/market/tickers — (symbol, bid1Price, ask1Price, markPrice, indexPrice, turnover24h)."""
    if payload.get("retCode") not in (0, "0"):
        raise RuntimeError(f"bybit tickers answer {payload.get('retCode')}: {payload.get('retMsg')}")
    rows = []
    for item in (payload.get("result") or {}).get("list") or []:
        symbol = item.get("symbol")
        if not symbol:
            continue
        number = lambda key: float(item[key]) if item.get(key) else None  # noqa: E731
        rows.append((symbol, float(item.get("bid1Price") or 0), float(item.get("ask1Price") or 0), number("markPrice"), number("indexPrice"), number("turnover24h")))
    return rows


class BybitMarket:
    def __init__(self, state: MarketState, poll_ms: int) -> None:
        self._state = state
        self._books = LocalBooks()
        self._buffer = BookBuffer(state, EXCHANGE)
        self._pool = DepthPool(
            EXCHANGE, WS_URL, lambda raw: handle_frame(self._books, self._buffer, self._state, raw), depth_messages,
            per_connection=SYMBOLS_PER_CONNECTION, batch_size=ARGS_PER_MESSAGE, send_interval_s=0.1, heartbeat=(20, PING),
        )
        self._poller = Poller("bybit tickers", poll_ms / 1000, self._poll)
        self._session: aiohttp.ClientSession | None = None

    def set_depth(self, symbols: Iterable[str]) -> None:
        self._pool.set_depth(symbols)

    def resubscribe_depth(self, symbol: str) -> None:
        self._pool.resubscribe(symbol)

    async def _poll(self) -> None:
        assert self._session is not None
        async with self._session.get(TICKERS_URL) as response:
            response.raise_for_status()
            payload = orjson.loads(await response.read())
        now = int(time.time() * 1000)
        for symbol, bid, ask, mark, index, volume in ticker_rows(payload):
            if bid > 0 and ask > 0:
                self._state.set_top(EXCHANGE, symbol, bid, ask, now)
            self._state.set_mark(EXCHANGE, symbol, mark, index, volume)

    async def run(self) -> None:
        self._pool.start()
        try:
            async with client_session(timeout=aiohttp.ClientTimeout(total=5)) as session:
                self._session = session
                await asyncio.gather(self._poller.run(), self._buffer.run())
        finally:
            self._pool.stop()

    def stats(self) -> dict[str, Any]:
        return {**self._pool.stats(), "polls": self._poller.polls, "poll_errors": self._poller.errors}
