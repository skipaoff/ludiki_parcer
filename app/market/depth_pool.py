"""
VFP: Shared plumbing of exchange market feeds — order-book subscriptions spread over websocket connections, books published to MarketState at a bounded rate, and REST polls on a fixed cadence.
Changes when: every feed needs the same new behaviour for placing subscriptions, publishing books or polling.
Anti-goal:
1. Exchange formats here — each feed builds its own messages and parses its own frames.
2. Converting every book frame — incremental books change dozens of times a second; converting each to Decimal levels
   cost more than it told, so a changed book is published at most once per publish interval.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Sequence

from app.market.state import MarketState
from app.market.ws import BuildMessages, ManagedSocket

log = logging.getLogger(__name__)


class DepthPool:
    """Order-book subscriptions of one exchange, placed on as many connections as the per-connection limit requires."""

    def __init__(
        self,
        prefix: str,
        url: str | Callable[[], Awaitable[str]],
        on_message: Callable[[str | bytes], str | None],
        build_messages: BuildMessages,
        per_connection: int,
        batch_size: int = 1,
        send_interval_s: float = 0.05,
        heartbeat: tuple[float, str] | None = None,
    ) -> None:
        self._prefix = prefix
        self._url = url
        self._on_message = on_message
        self._build = build_messages
        self._per_connection = per_connection
        self._batch_size = batch_size
        self._send_interval_s = send_interval_s
        self._heartbeat = heartbeat
        self.sockets: list[ManagedSocket] = []
        self._placement: dict[str, ManagedSocket] = {}
        self._tasks: dict[ManagedSocket, asyncio.Task] = {}

    def set_depth(self, symbols: Iterable[str]) -> None:
        wanted = list(dict.fromkeys(symbols))
        for symbol in [symbol for symbol in self._placement if symbol not in wanted]:
            del self._placement[symbol]
        for symbol in wanted:
            if symbol in self._placement:
                continue
            socket = next((s for s in self.sockets if self._load(s) < self._per_connection), None)
            if socket is None:
                socket = ManagedSocket(
                    f"{self._prefix}-depth-{len(self.sockets) + 1}", self._url, self._on_message, self._build,
                    batch_size=self._batch_size, send_interval_s=self._send_interval_s, heartbeat=self._heartbeat,
                )
                self.sockets.append(socket)
                self._start(socket)
            self._placement[symbol] = socket
        for socket in self.sockets:
            socket.set_desired(symbol for symbol, placed in self._placement.items() if placed is socket)

    def resubscribe(self, symbol: str) -> None:
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
            pass  # not running yet; start() does it

    def start(self) -> None:
        for socket in self.sockets:
            self._start(socket)

    def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()

    def stats(self) -> dict[str, Any]:
        return {
            "connections": sum(1 for socket in self.sockets if socket.connected),
            "sockets": len(self.sockets),
            "depth_symbols": len(self._placement),
            "reconnects": sum(socket.reconnects for socket in self.sockets),
        }


class BookBuffer:
    """Latest raw book per symbol; changed books go to MarketState at most once per interval."""

    def __init__(self, state: MarketState, exchange: str, interval_s: float = 0.1) -> None:
        self._state = state
        self._exchange = exchange
        self._interval_s = interval_s
        self._pending: dict[str, tuple[Sequence[Sequence[Any]], Sequence[Sequence[Any]], int]] = {}

    def put(self, symbol: str, bids: Sequence[Sequence[Any]], asks: Sequence[Sequence[Any]], exchange_ts_ms: int) -> None:
        self._pending[symbol] = (bids, asks, exchange_ts_ms)

    def flush(self) -> int:
        pending, self._pending = self._pending, {}
        for symbol, (bids, asks, ts) in pending.items():
            self._state.set_book(self._exchange, symbol, bids, asks, ts)
        return len(pending)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            self.flush()


class Poller:
    """One REST poll repeated on a fixed cadence; failures are counted and logged at most every 30 s."""

    def __init__(self, name: str, interval_s: float, call: Callable[[], Awaitable[None]]) -> None:
        self.name = name
        self._interval_s = interval_s
        self._call = call
        self.polls = 0
        self.errors = 0
        self._last_error_log = 0.0

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._call()
                self.polls += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                if time.monotonic() - self._last_error_log > 30:
                    self._last_error_log = time.monotonic()
                    log.warning("%s poll failed: %s: %s", self.name, type(exc).__name__, exc)
            await asyncio.sleep(max(0.0, self._interval_s - (time.monotonic() - started)))
