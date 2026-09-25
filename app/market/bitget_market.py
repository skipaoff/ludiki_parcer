"""
VFP: Bitget USDT-M futures market data into MarketState — best prices, mark, index and volume of every contract from REST, order books of candidates from websocket snapshots.
Changes when: Bitget changes its v2 tickers or books channels.
Anti-goal:
1. Incremental book channels — books15 pushes a full 15-level snapshot about 9 times a second (checked live 15.09.2026),
   so there is no local book to keep in step.
2. A silent socket — Bitget closes connections that send no "ping" for 2 minutes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.system.tls import client_session
from app.market.depth_pool import DepthPool, Poller
from app.market.state import MarketState

WS_URL = "wss://ws.bitget.com/v2/ws/public"
TICKERS_URL = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
SYMBOLS_PER_CONNECTION = 50
ARGS_PER_MESSAGE = 10
EXCHANGE = "bitget"
CHANNEL = "books15"


def depth_messages(symbols: list[str], subscribe: bool) -> Iterable[str]:
    args = [{"instType": "USDT-FUTURES", "channel": CHANNEL, "instId": symbol} for symbol in symbols]
    yield orjson.dumps({"op": "subscribe" if subscribe else "unsubscribe", "args": args}).decode()


def handle_frame(state: MarketState, raw: str | bytes) -> None:
    if raw == "pong":
        return
    message: dict[str, Any] = orjson.loads(raw)
    arg = message.get("arg") or {}
    if arg.get("channel") != CHANNEL or not message.get("data"):
        return
    data = message["data"][0]
    state.count(EXCHANGE)
    state.set_book(EXCHANGE, arg["instId"], data.get("bids") or [], data.get("asks") or [], int(data.get("ts") or message.get("ts") or 0))


def ticker_rows(payload: dict[str, Any]) -> list[tuple[str, float, float, float | None, float | None, float | None]]:
    """GET /api/v2/mix/market/tickers — (symbol, bidPr, askPr, markPrice, indexPrice, usdtVolume)."""
    if str(payload.get("code")) != "00000":
        raise RuntimeError(f"bitget tickers answer {payload.get('code')}: {payload.get('msg')}")
    rows = []
    for item in payload.get("data") or []:
        symbol = item.get("symbol")
        if not symbol:
            continue
        number = lambda key: float(item[key]) if item.get(key) else None  # noqa: E731
        rows.append((symbol, float(item.get("bidPr") or 0), float(item.get("askPr") or 0), number("markPrice"), number("indexPrice"), number("usdtVolume")))
    return rows


class BitgetMarket:
    def __init__(self, state: MarketState, poll_ms: int) -> None:
        self._state = state
        self._pool = DepthPool(
            EXCHANGE, WS_URL, lambda raw: handle_frame(self._state, raw), depth_messages,
            per_connection=SYMBOLS_PER_CONNECTION, batch_size=ARGS_PER_MESSAGE, send_interval_s=0.1, heartbeat=(25, "ping"),
        )
        self._poller = Poller("bitget tickers", poll_ms / 1000, self._poll)
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
        self._state.count(EXCHANGE)

    async def run(self) -> None:
        self._pool.start()
        try:
            async with client_session(timeout=aiohttp.ClientTimeout(total=5)) as session:
                self._session = session
                await self._poller.run()
        finally:
            self._pool.stop()

    def stats(self) -> dict[str, Any]:
        return {**self._pool.stats(), "polls": self._poller.polls, "poll_errors": self._poller.errors}
