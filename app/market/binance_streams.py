"""
VFP: Binance-format futures market data into MarketState — best prices of every pair for the radar, depth20@100ms for candidates. Serves Binance USDⓈ-M and Aster, which speaks the same protocol.
Changes when: Binance or Aster stream routes, payloads or per-connection limits change.
Anti-goal:
1. Binance's all-symbols !bookTicker stream — it updates only every 5 seconds there.
2. More than 200 streams on one connection — both exchanges refuse them.
3. Streaming best prices per symbol — Aster's book tickers ran at 6,600 messages a second (15.09.2026), a third of a CPU
   core and stalls of the event loop of up to 300 ms. Binance's arrived here 2 s late on the median and up to 4 s even for
   ten symbols (depth20@100ms on the same route: on time), and a socket with 200 of them was dropped every ~20 s without a
   close frame (15.09.2026). Both radars poll the all-symbols REST book ticker once a second instead (Binance: 22 KB
   gzipped, ~0.3 s, weight 5 of 2,400 a minute).

Payloads checked live: Binance on 13.09.2026, Aster on 14.09.2026 (docs/EXCHANGES.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.market.state import MarketState
from app.market.ws import ManagedSocket

log = logging.getLogger(__name__)

PUBLIC_URL = "wss://fstream.binance.com/public/stream"
MARKET_URL = "wss://fstream.binance.com/market/stream"
MARK_PRICE_STREAM = "!markPrice@arr@1s"
RADAR_POLL_S = 1.0
BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
ASTER_URL = "wss://fstream.asterdex.com/stream"
ASTER_BOOK_TICKER_URL = "https://fapi.asterdex.com/fapi/v1/ticker/bookTicker"
SEED_INTERVAL_S = 30
STREAMS_PER_CONNECTION = 200
PARAMS_PER_MESSAGE = 50
EXCHANGE = "binance"


def book_ticker_stream(symbol: str) -> str:
    return f"{symbol.lower()}@bookTicker"


def depth_stream(symbol: str) -> str:
    return f"{symbol.lower()}@depth20@100ms"


def control_messages(streams: list[str], subscribe: bool) -> Iterable[str]:
    yield orjson.dumps({"method": "SUBSCRIBE" if subscribe else "UNSUBSCRIBE", "params": streams, "id": 1}).decode()


def handle_frame(state: MarketState, raw: str | bytes, exchange: str = EXCHANGE) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    data = message.get("data")
    if data is None:
        if message.get("error"):
            log.warning("%s stream error: %s", exchange, message["error"])
        return
    if isinstance(data, list):
        # !markPrice@arr@1s: every symbol once a second.
        for item in data:
            if item.get("e") == "markPriceUpdate":
                state.set_mark(exchange, item["s"], float(item["p"]), float(item["i"]) if item.get("i") else None)
        return
    state.count(exchange)
    kind = data.get("e")
    if kind == "bookTicker":
        state.set_top(exchange, data["s"], float(data["b"]), float(data["a"]), int(data.get("T") or data.get("E") or 0))
    elif kind == "depthUpdate":
        state.set_book(exchange, data["s"], data["b"], data["a"], int(data.get("T") or data.get("E") or 0))


class BinanceStreams:
    def __init__(
        self,
        state: MarketState,
        exchange: str = EXCHANGE,
        public_url: str = PUBLIC_URL,
        market_url: str = MARKET_URL,
        book_ticker_url: str = BOOK_TICKER_URL,
        radar_poll_s: float | None = None,
    ) -> None:
        """radar_poll_s: take radar prices from the REST book ticker this often instead of a stream per symbol."""
        self._state = state
        self._exchange = exchange
        self._public_url = public_url
        self._book_ticker_url = book_ticker_url
        self._radar_poll_s = radar_poll_s
        self._radar: list[ManagedSocket] = []
        self._depth = self._socket(f"{exchange}-depth")
        self._marks = self._socket(f"{exchange}-marks", market_url)
        self._marks.set_desired([MARK_PRICE_STREAM])
        self._tasks: dict[ManagedSocket, asyncio.Task] = {}
        self._radar_symbols: list[str] = []
        self.polls = 0

    def _socket(self, name: str, url: str = "") -> ManagedSocket:
        return ManagedSocket(
            name,
            url or self._public_url,
            lambda raw: handle_frame(self._state, raw, self._exchange),
            control_messages,
            batch_size=PARAMS_PER_MESSAGE,
            send_interval_s=0.25,  # both exchanges allow 10 control messages per second per connection
        )

    def set_radar(self, symbols: Iterable[str]) -> None:
        ordered = sorted(set(symbols))
        if ordered == self._radar_symbols:
            return
        self._radar_symbols = ordered
        if self._radar_poll_s is not None:
            return
        chunks = [ordered[i : i + STREAMS_PER_CONNECTION] for i in range(0, len(ordered), STREAMS_PER_CONNECTION)]
        while len(self._radar) < len(chunks):
            socket = self._socket(f"{self._exchange}-radar-{len(self._radar) + 1}")
            self._radar.append(socket)
            self._start(socket)
        for index, socket in enumerate(self._radar):
            chunk = chunks[index] if index < len(chunks) else []
            socket.set_desired(book_ticker_stream(symbol) for symbol in chunk)

    def set_depth(self, symbols: Iterable[str]) -> None:
        self._depth.set_desired(depth_stream(symbol) for symbol in list(symbols)[:STREAMS_PER_CONNECTION])

    def resubscribe_depth(self, symbol: str) -> None:
        self._depth.resubscribe(depth_stream(symbol))

    def stream_age_ms(self, symbol: str) -> float | None:
        """Time since the depth connection delivered any frame; a quiet book on a live connection is not stale."""
        if not self._depth.connected or self._depth.last_message_ms is None:
            return None
        return time.time() * 1000 - self._depth.last_message_ms

    async def run(self) -> None:
        self._start(self._depth)
        self._start(self._marks)
        for socket in self._radar:
            self._start(socket)
        try:
            await self._seed_tops()
        finally:
            for task in self._tasks.values():
                task.cancel()

    async def _seed_tops(self) -> None:
        """
        bookTicker streams send nothing until prices change, so quiet contracts are seeded from REST; when the radar is
        polled, this is the radar. A quote no older than the one held confirms it is still current.
        """
        timeout = aiohttp.ClientTimeout(total=10)
        interval = SEED_INTERVAL_S if self._radar_poll_s is None else self._radar_poll_s
        last_error_log = 0.0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                started = time.monotonic()
                try:
                    async with session.get(self._book_ticker_url) as response:
                        response.raise_for_status()
                        for item in orjson.loads(await response.read()):
                            key = (self._exchange, item["symbol"])
                            ts = int(item.get("time") or 0)
                            current = self._state.tops.get(key)
                            if current is None or current.exchange_ts_ms <= ts:
                                self._state.set_top(self._exchange, item["symbol"], float(item["bidPrice"]), float(item["askPrice"]), ts)
                    self._state.count(self._exchange)
                    self.polls += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if time.monotonic() - last_error_log > 30:
                        last_error_log = time.monotonic()
                        log.warning("%s book ticker poll failed: %s: %s", self._exchange, type(exc).__name__, exc)
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    def _start(self, socket: ManagedSocket) -> None:
        if socket in self._tasks:
            return
        try:
            self._tasks[socket] = asyncio.get_running_loop().create_task(socket.run())
        except RuntimeError:
            pass  # not running yet; run() starts it

    def stats(self) -> dict[str, Any]:
        sockets = [self._depth, self._marks, *self._radar]
        return {
            "connections": sum(1 for socket in sockets if socket.connected),
            "sockets": len(sockets),
            "radar_streams": sum(len(socket.desired) for socket in self._radar),
            "polls": self.polls,
            "depth_streams": len(self._depth.desired),
            "reconnects": sum(socket.reconnects for socket in sockets),
        }


def binance_streams(state: MarketState) -> BinanceStreams:
    """Binance: books and marks streamed, radar prices polled from REST once a second (see anti-goal 3)."""
    return BinanceStreams(state, radar_poll_s=RADAR_POLL_S)


def aster_streams(state: MarketState) -> BinanceStreams:
    """Aster: one host for every stream; radar prices polled from REST once a second (see anti-goal 3)."""
    return BinanceStreams(
        state,
        exchange="aster",
        public_url=ASTER_URL,
        market_url=ASTER_URL,
        book_ticker_url=ASTER_BOOK_TICKER_URL,
        radar_poll_s=RADAR_POLL_S,
    )
