"""
VFP: BingX perpetual swap market data into MarketState — best prices and volume of every contract from REST, mark and index from REST, order books of candidates from websockets.
Changes when: BingX changes its swap ticker, premium index or depth channels.
Anti-goal:
1. The websocket bookTicker channel for best prices — it lagged and stood wider than the order book, while the REST
   ticker matched the websocket depth top 58 times out of 60 (checked 14.09.2026).
2. More than 200 topics on one connection — BingX refuses the 201st with code 80403.
3. A silent answer to server pings — BingX sends "Ping" and drops connections that do not answer "Pong".

Frames arrive gzip-compressed. Depth levels are [price, quantity in coins of the contract].
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.system.tls import client_session
from app.market.state import MarketState
from app.market.ws import ManagedSocket

log = logging.getLogger(__name__)

WS_URL = "wss://open-api-swap.bingx.com/swap-market"
TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
PREMIUM_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
SYMBOLS_PER_CONNECTION = 50
EXCHANGE = "bingx"


def depth_topic(symbol: str) -> str:
    return f"{symbol}@depth20@100ms"


def depth_messages(symbols: list[str], subscribe: bool) -> Iterable[str]:
    for symbol in symbols:
        topic = depth_topic(symbol)
        yield orjson.dumps({"id": f"{'s' if subscribe else 'u'}:{topic}", "reqType": "sub" if subscribe else "unsub", "dataType": topic}).decode()


def decode(raw: str | bytes) -> str:
    return gzip.decompress(raw).decode() if isinstance(raw, bytes) else raw


def handle_frame(state: MarketState, raw: str | bytes) -> str | None:
    """Apply one websocket frame; returns the answer to send back, if any."""
    text = decode(raw)
    if text == "Ping":
        return "Pong"
    message: dict[str, Any] = orjson.loads(text)
    topic = message.get("dataType") or ""
    data = message.get("data")
    if topic.endswith("@depth20@100ms") and isinstance(data, dict):
        state.set_book(EXCHANGE, topic.split("@", 1)[0], data.get("bids") or [], data.get("asks") or [], int(message.get("ts") or 0))
    elif message.get("code") not in (None, 0):
        log.warning("bingx stream answer %s: %s %s", message.get("id"), message.get("code"), message.get("msg"))
    return None


def ticker_rows(payload: dict[str, Any]) -> list[tuple[str, float, float, float | None]]:
    """GET /openApi/swap/v2/quote/ticker — (symbol, bidPrice, askPrice, quoteVolume) for every contract."""
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(f"bingx ticker answer code {payload.get('code')}: {payload.get('msg')}")
    rows = []
    for item in payload.get("data") or []:
        symbol = item.get("symbol")
        if not symbol:
            continue
        volume = item.get("quoteVolume")
        rows.append((symbol, float(item.get("bidPrice") or 0), float(item.get("askPrice") or 0), float(volume) if volume else None))
    return rows


def premium_rows(payload: dict[str, Any]) -> list[tuple[str, float | None, float | None]]:
    """GET /openApi/swap/v2/quote/premiumIndex — (symbol, markPrice, indexPrice) for every contract."""
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(f"bingx premium index answer code {payload.get('code')}: {payload.get('msg')}")
    return [
        (item["symbol"], float(item["markPrice"]) if item.get("markPrice") else None, float(item["indexPrice"]) if item.get("indexPrice") else None)
        for item in payload.get("data") or []
        if item.get("symbol")
    ]


class BingxMarket:
    def __init__(self, state: MarketState, ticker_poll_ms: int, premium_poll_ms: int) -> None:
        self._state = state
        self._ticker_poll_s = ticker_poll_ms / 1000
        self._premium_poll_s = premium_poll_ms / 1000
        self._sockets: list[ManagedSocket] = []
        self._placement: dict[str, ManagedSocket] = {}
        self._tasks: dict[ManagedSocket, asyncio.Task] = {}
        self._volumes: dict[str, float | None] = {}
        self.polls = 0
        self.poll_errors = 0
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
                    f"bingx-depth-{len(self._sockets) + 1}",
                    WS_URL,
                    lambda raw: handle_frame(self._state, raw),
                    depth_messages,
                    batch_size=1,
                    send_interval_s=0.05,
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

    def _load(self, socket: ManagedSocket) -> int:
        return sum(1 for placed in self._placement.values() if placed is socket)

    def _start(self, socket: ManagedSocket) -> None:
        if socket in self._tasks:
            return
        try:
            self._tasks[socket] = asyncio.get_running_loop().create_task(socket.run())
        except RuntimeError:
            pass

    async def run(self) -> None:
        for socket in self._sockets:
            self._start(socket)
        try:
            async with client_session(timeout=aiohttp.ClientTimeout(total=5)) as session:
                await asyncio.gather(self._poll_tickers(session), self._poll_premium(session))
        finally:
            for task in self._tasks.values():
                task.cancel()

    async def _poll_tickers(self, session: aiohttp.ClientSession) -> None:
        while True:
            started = time.monotonic()
            try:
                async with session.get(TICKER_URL) as response:
                    response.raise_for_status()
                    payload = orjson.loads(await response.read())
                now = int(time.time() * 1000)
                for symbol, bid, ask, volume in ticker_rows(payload):
                    if bid > 0 and ask > 0:
                        self._state.set_top(EXCHANGE, symbol, bid, ask, now)
                    self._volumes[symbol] = volume
                self.polls += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._poll_failed("ticker", exc)
            await asyncio.sleep(max(0.0, self._ticker_poll_s - (time.monotonic() - started)))

    async def _poll_premium(self, session: aiohttp.ClientSession) -> None:
        while True:
            started = time.monotonic()
            try:
                async with session.get(PREMIUM_URL) as response:
                    response.raise_for_status()
                    payload = orjson.loads(await response.read())
                for symbol, mark, index in premium_rows(payload):
                    self._state.set_mark(EXCHANGE, symbol, mark, index, self._volumes.get(symbol))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._poll_failed("premium index", exc)
            await asyncio.sleep(max(0.0, self._premium_poll_s - (time.monotonic() - started)))

    def _poll_failed(self, what: str, exc: Exception) -> None:
        self.poll_errors += 1
        if time.monotonic() - self._last_error_log > 30:
            self._last_error_log = time.monotonic()
            log.warning("bingx %s poll failed: %s: %s", what, type(exc).__name__, exc)

    def stats(self) -> dict[str, Any]:
        return {
            "connections": sum(1 for socket in self._sockets if socket.connected),
            "sockets": len(self._sockets),
            "depth_symbols": len(self._placement),
            "reconnects": sum(socket.reconnects for socket in self._sockets),
            "polls": self.polls,
            "poll_errors": self.poll_errors,
        }
