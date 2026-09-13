"""
VFP: Binance USDⓈ-M market streams into MarketState — bookTicker of every pair for the radar, depth20@100ms for candidates.
Changes when: Binance stream routes, payloads or per-connection limits change.
Anti-goal:
1. The all-symbols !bookTicker stream — it updates only every 5 seconds.
2. More than 200 streams on one connection — Binance futures refuses them.

Payloads checked live on 13.09.2026 (docs/EXCHANGES.md).
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
BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
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


def handle_frame(state: MarketState, raw: str | bytes) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    data = message.get("data")
    if data is None:
        if message.get("error"):
            log.warning("binance stream error: %s", message["error"])
        return
    state.count(EXCHANGE)
    kind = data.get("e")
    if kind == "bookTicker":
        state.set_top(EXCHANGE, data["s"], float(data["b"]), float(data["a"]), int(data.get("T") or data.get("E") or 0))
    elif kind == "depthUpdate":
        state.set_book(EXCHANGE, data["s"], data["b"], data["a"], int(data.get("T") or data.get("E") or 0))


class BinanceStreams:
    def __init__(self, state: MarketState) -> None:
        self._state = state
        self._radar: list[ManagedSocket] = []
        self._depth = self._socket("binance-depth")
        self._tasks: dict[ManagedSocket, asyncio.Task] = {}
        self._radar_symbols: list[str] = []

    def _socket(self, name: str) -> ManagedSocket:
        return ManagedSocket(
            name,
            PUBLIC_URL,
            lambda raw: handle_frame(self._state, raw),
            control_messages,
            batch_size=PARAMS_PER_MESSAGE,
            send_interval_s=0.25,  # Binance allows 10 control messages per second per connection
        )

    def set_radar(self, symbols: Iterable[str]) -> None:
        ordered = sorted(set(symbols))
        if ordered == self._radar_symbols:
            return
        self._radar_symbols = ordered
        chunks = [ordered[i : i + STREAMS_PER_CONNECTION] for i in range(0, len(ordered), STREAMS_PER_CONNECTION)]
        while len(self._radar) < len(chunks):
            socket = self._socket(f"binance-radar-{len(self._radar) + 1}")
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
        for socket in self._radar:
            self._start(socket)
        try:
            await self._seed_tops()
        finally:
            for task in self._tasks.values():
                task.cancel()

    async def _seed_tops(self) -> None:
        """bookTicker streams send nothing until prices change, so quiet contracts are seeded from REST."""
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    async with session.get(BOOK_TICKER_URL) as response:
                        for item in orjson.loads(await response.read()):
                            key = (EXCHANGE, item["symbol"])
                            ts = int(item.get("time") or 0)
                            current = self._state.tops.get(key)
                            if current is None or current.exchange_ts_ms < ts:
                                self._state.set_top(EXCHANGE, item["symbol"], float(item["bidPrice"]), float(item["askPrice"]), ts)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("binance book ticker seed failed: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(SEED_INTERVAL_S)

    def _start(self, socket: ManagedSocket) -> None:
        if socket in self._tasks:
            return
        try:
            self._tasks[socket] = asyncio.get_running_loop().create_task(socket.run())
        except RuntimeError:
            pass  # not running yet; run() starts it

    def stats(self) -> dict[str, Any]:
        sockets = [self._depth, *self._radar]
        return {
            "connections": sum(1 for socket in sockets if socket.connected),
            "sockets": len(sockets),
            "radar_streams": sum(len(socket.desired) for socket in self._radar),
            "depth_streams": len(self._depth.desired),
            "reconnects": sum(socket.reconnects for socket in sockets),
        }
