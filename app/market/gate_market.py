"""
VFP: Gate USDT futures market data into MarketState — best prices, mark, index and volume of every contract from REST, order books of candidates from websockets.
Changes when: Gate changes its futures tickers or order book channels.
Anti-goal:
1. Reading book sizes as tokens — Gate sends sizes in contracts; MarketState converts with the contract multiplier.
2. Applying incremental book events as snapshots — only full "all" snapshots are applied (checked live on 14.09.2026:
   futures.order_book with interval "0" pushes full 20-level snapshots about 9 times a second).
3. A silent socket — Gate closes connections without application pings.
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

WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
TICKERS_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
TICKERS_TIMEOUT_S = 20
SYMBOLS_PER_CONNECTION = 30
DEPTH_LEVELS = "20"
EXCHANGE = "gate"


def _control(channel: str, event: str, payload: list[str]) -> str:
    return orjson.dumps({"time": int(time.time()), "channel": channel, "event": event, "payload": payload}).decode()


def depth_messages(symbols: list[str], subscribe: bool) -> Iterable[str]:
    for symbol in symbols:
        yield _control("futures.order_book", "subscribe" if subscribe else "unsubscribe", [symbol, DEPTH_LEVELS, "0"])


def ping_message() -> str:
    return orjson.dumps({"time": int(time.time()), "channel": "futures.ping"}).decode()


def handle_frame(state: MarketState, raw: str | bytes) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    channel, event = message.get("channel"), message.get("event")
    if channel == "futures.order_book" and event == "all":
        result = message.get("result") or {}
        state.count(EXCHANGE)
        state.set_book(
            EXCHANGE,
            result["contract"],
            [[level["p"], level["s"]] for level in result.get("bids") or []],
            [[level["p"], level["s"]] for level in result.get("asks") or []],
            int(result.get("t") or message.get("time_ms") or 0),
        )
    elif message.get("error"):
        log.warning("gate stream %s %s error: %s", channel, event, message.get("error"))


def ticker_rows(tickers: list[dict[str, Any]]) -> list[tuple[str, float, float, float | None, float | None, float | None]]:
    """(contract, highest_bid, lowest_ask, mark, index, 24h USDT turnover) for every contract."""
    rows = []
    for item in tickers:
        contract = item.get("contract")
        if not contract:
            continue
        rows.append(
            (
                contract,
                float(item.get("highest_bid") or 0),
                float(item.get("lowest_ask") or 0),
                float(item["mark_price"]) if item.get("mark_price") else None,
                float(item["index_price"]) if item.get("index_price") else None,
                float(item["volume_24h_quote"]) if item.get("volume_24h_quote") else None,
            )
        )
    return rows


class GateMarket:
    def __init__(self, state: MarketState, poll_ms: int) -> None:
        self._state = state
        self._poll_s = poll_ms / 1000
        self._sockets: list[ManagedSocket] = []
        self._placement: dict[str, ManagedSocket] = {}
        self._tasks: dict[ManagedSocket, asyncio.Task] = {}
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
                    f"gate-depth-{len(self._sockets) + 1}",
                    WS_URL,
                    lambda raw: handle_frame(self._state, raw),
                    depth_messages,
                    batch_size=1,
                    send_interval_s=0.05,
                    heartbeat=(15, ping_message()),
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
            await self._poll_tickers()
        finally:
            for task in self._tasks.values():
                task.cancel()

    async def _poll_tickers(self) -> None:
        # The list is 0.5 MB and can take several seconds to arrive; its prices are dated from the request, not the arrival.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TICKERS_TIMEOUT_S)) as session:
            while True:
                started = time.monotonic()
                try:
                    now = int(time.time() * 1000)
                    async with session.get(TICKERS_URL) as response:
                        response.raise_for_status()
                        tickers = orjson.loads(await response.read())
                    for contract, bid, ask, mark, index, volume in ticker_rows(tickers):
                        if bid > 0 and ask > 0:
                            self._state.set_top(EXCHANGE, contract, bid, ask, now, received_ms=now)
                        self._state.set_mark(EXCHANGE, contract, mark, index, volume)
                    self._state.count(EXCHANGE)
                    self.polls += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.poll_errors += 1
                    if time.monotonic() - self._last_error_log > 30:
                        self._last_error_log = time.monotonic()
                        log.warning("gate ticker poll failed: %s: %s", type(exc).__name__, exc)
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
