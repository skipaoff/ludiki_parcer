"""
VFP: MEXC contract market data into MarketState — best prices of every contract from REST, order books of candidates from websockets.
Changes when: MEXC changes its contract ticker or depth channels.
Anti-goal:
1. push.tickers for best prices — that channel carries no bid/ask, only last, index and price limits (checked 13.09.2026).
2. Reading depth levels as [price, orders, volume] — MEXC sends [price, volume in contracts, order count].
3. A silent socket — MEXC drops connections that send no ping within a minute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.system.tls import client_session
from app.market.state import MarketState
from app.market.ws import ManagedSocket

log = logging.getLogger(__name__)

WS_URL = "wss://contract.mexc.com/edge"
TICKER_URL = "https://api.mexc.com/api/v1/contract/ticker"
SYMBOLS_PER_CONNECTION = 30
DEPTH_LIMIT = 20
EXCHANGE = "mexc"
PING = orjson.dumps({"method": "ping"}).decode()


def depth_messages(symbols: list[str], subscribe: bool) -> Iterable[str]:
    for symbol in symbols:
        if subscribe:
            yield orjson.dumps({"method": "sub.depth.full", "param": {"symbol": symbol, "limit": DEPTH_LIMIT}}).decode()
        else:
            yield orjson.dumps({"method": "unsub.depth.full", "param": {"symbol": symbol, "limit": DEPTH_LIMIT}}).decode()


def handle_frame(state: MarketState, raw: str | bytes) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    channel = message.get("channel")
    if channel == "push.depth.full":
        data = message["data"]
        state.count(EXCHANGE)
        state.set_book(EXCHANGE, message["symbol"], data["bids"], data["asks"], int(data.get("cts") or message.get("ts") or 0))
    elif channel == "rs.error" or (isinstance(channel, str) and channel.startswith("rs.") and message.get("data") != "success"):
        log.warning("mexc stream answer %s: %s", channel, message.get("data"))


def ticker_tops(payload: dict[str, Any]) -> list[tuple[str, float, float, int]]:
    """GET /api/v1/contract/ticker — (symbol, bid1, ask1, timestamp) of contracts with both prices."""
    return [(symbol, bid, ask, ts) for symbol, bid, ask, ts, *_ in ticker_rows(payload) if bid and ask]


def ticker_rows(payload: dict[str, Any]) -> list[tuple[str, float, float, int, float | None, float | None, float | None]]:
    """(symbol, bid1, ask1, timestamp, fairPrice, indexPrice, amount24) for every contract in the ticker answer."""
    if not payload.get("success", False):
        raise RuntimeError(f"mexc ticker answer code {payload.get('code')}")
    rows = []
    for item in payload.get("data") or []:
        rows.append(
            (
                item["symbol"],
                float(item.get("bid1") or 0),
                float(item.get("ask1") or 0),
                int(item.get("timestamp") or 0),
                float(item["fairPrice"]) if item.get("fairPrice") else None,
                float(item["indexPrice"]) if item.get("indexPrice") else None,
                float(item["amount24"]) if item.get("amount24") is not None else None,
            )
        )
    return rows


class MexcMarket:
    def __init__(self, state: MarketState, poll_ms: int) -> None:
        self._state = state
        self._poll_s = poll_ms / 1000
        self._sockets: list[ManagedSocket] = []
        self._placement: dict[str, ManagedSocket] = {}
        self._tasks: dict[ManagedSocket, asyncio.Task] = {}
        self.polls = 0
        self.poll_errors = 0
        self.last_poll_ms: float | None = None
        self._last_error_log = 0.0

    def set_depth(self, symbols: Iterable[str]) -> None:
        wanted = list(dict.fromkeys(symbols))
        for symbol in [symbol for symbol in self._placement if symbol not in wanted]:
            del self._placement[symbol]
        for symbol in wanted:
            if symbol in self._placement:
                continue
            socket = next((s for s in self._sockets if self._load(s) < SYMBOLS_PER_CONNECTION), None)
            if socket is None:
                socket = ManagedSocket(
                    f"mexc-depth-{len(self._sockets) + 1}",
                    WS_URL,
                    lambda raw: handle_frame(self._state, raw),
                    depth_messages,
                    batch_size=1,
                    send_interval_s=0.05,
                    heartbeat=(15, PING),
                )
                self._sockets.append(socket)
                self._start(socket)
            self._placement[symbol] = socket
        for socket in self._sockets:
            socket.set_desired(symbol for symbol, placed in self._placement.items() if placed is socket)

    def resubscribe_depth(self, symbol: str) -> None:
        socket = self._placement.get(symbol)
        if socket is not None:
            socket.resubscribe(symbol)

    def stream_age_ms(self, symbol: str) -> float | None:
        """Time since the connection carrying this symbol's book delivered any frame (pongs included)."""
        socket = self._placement.get(symbol)
        if socket is None or not socket.connected or socket.last_message_ms is None:
            return None
        return time.time() * 1000 - socket.last_message_ms

    def _load(self, socket: ManagedSocket) -> int:
        return sum(1 for placed in self._placement.values() if placed is socket)

    async def run(self) -> None:
        for socket in self._sockets:
            self._start(socket)
        try:
            await self._poll_tickers()
        finally:
            for task in self._tasks.values():
                task.cancel()

    def _start(self, socket: ManagedSocket) -> None:
        if socket in self._tasks:
            return
        try:
            self._tasks[socket] = asyncio.get_running_loop().create_task(socket.run())
        except RuntimeError:
            pass

    async def _poll_tickers(self) -> None:
        timeout = aiohttp.ClientTimeout(total=5)
        async with client_session(timeout=timeout) as session:
            while True:
                started = time.monotonic()
                try:
                    async with session.get(TICKER_URL) as response:
                        payload = orjson.loads(await response.read())
                    for symbol, bid, ask, ts, fair, index, amount in ticker_rows(payload):
                        if bid and ask:
                            self._state.set_top(EXCHANGE, symbol, bid, ask, ts)
                        self._state.set_mark(EXCHANGE, symbol, fair, index, amount)
                    self._state.count(EXCHANGE)
                    self.polls += 1
                    self.last_poll_ms = time.time() * 1000
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.poll_errors += 1
                    if time.monotonic() - self._last_error_log > 30:
                        self._last_error_log = time.monotonic()
                        log.warning("mexc ticker poll failed: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(max(0.0, self._poll_s - (time.monotonic() - started)))

    def stats(self) -> dict[str, Any]:
        return {
            "connections": sum(1 for socket in self._sockets if socket.connected),
            "sockets": len(self._sockets),
            "depth_symbols": len(self._placement),
            "reconnects": sum(socket.reconnects for socket in self._sockets),
            "polls": self.polls,
            "poll_errors": self.poll_errors,
        }
