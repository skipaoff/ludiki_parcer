"""
VFP: One exchange websocket that stays connected and keeps its subscriptions equal to a desired set, whatever drops in between.
Changes when: reconnect, heartbeat or subscription-sync behaviour changes for all exchanges.
Anti-goal:
1. Exchange message formats here — callers build subscribe messages and parse incoming frames.
2. A parsing error killing the connection — handler failures are logged (rate-limited) and reading continues.
3. Flooding an exchange with control messages — subscription changes are batched and paced.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Iterable

import websockets

from app.system.tls import shared_context

log = logging.getLogger(__name__)

BuildMessages = Callable[[list[str], bool], Iterable[str]]
MAX_BACKOFF_S = 30


class ManagedSocket:
    def __init__(
        self,
        name: str,
        url: str | Callable[[], Awaitable[str]],
        on_message: Callable[[str | bytes], str | None],
        build_messages: BuildMessages,
        batch_size: int,
        send_interval_s: float,
        heartbeat: tuple[float, str] | None = None,
    ) -> None:
        """url may be a coroutine function for exchanges that hand out a fresh address with a token per connection."""
        self.name = name
        self.url = url
        self._on_message = on_message
        self._build = build_messages
        self._batch_size = batch_size
        self._send_interval_s = send_interval_s
        self._heartbeat = heartbeat
        self.desired: set[str] = set()
        self._subscribed: set[str] = set()
        self._wake = asyncio.Event()
        self.connected = False
        self.reconnects = 0
        self.last_message_ms: float | None = None
        self._last_error_log = 0.0

    def set_desired(self, items: Iterable[str]) -> None:
        self.desired = set(items)
        self._wake.set()

    def resubscribe(self, item: str) -> None:
        """Send the subscription again, e.g. when its data went stale while the socket stayed open."""
        self._subscribed.discard(item)
        self._wake.set()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                url = self.url if isinstance(self.url, str) else await self.url()
                ssl = shared_context() if url.startswith("wss:") else None
                async with websockets.connect(url, max_size=2**24, open_timeout=10, close_timeout=2, ssl=ssl) as socket:
                    self.connected = True
                    backoff = 1.0
                    self._subscribed = set()
                    helpers = [asyncio.create_task(self._sync(socket))]
                    if self._heartbeat:
                        helpers.append(asyncio.create_task(self._beat(socket, *self._heartbeat)))
                    try:
                        async for raw in socket:
                            self.last_message_ms = time.time() * 1000
                            try:
                                reply = self._on_message(raw)
                            except Exception:
                                self._log_handler_error()
                                continue
                            if reply is not None:
                                # Exchanges with application-level server pings (BingX "Ping") expect an answer.
                                await socket.send(reply)
                    finally:
                        for helper in helpers:
                            helper.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s disconnected: %s: %s", self.name, type(exc).__name__, exc)
            self.connected = False
            self._subscribed = set()
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _sync(self, socket) -> None:
        while True:
            self._wake.clear()
            remove = sorted(self._subscribed - self.desired)
            add = sorted(self.desired - self._subscribed)
            if not remove and not add:
                await self._wake.wait()
                continue
            for items, subscribe in ((remove, False), (add, True)):
                for start in range(0, len(items), self._batch_size):
                    batch = items[start : start + self._batch_size]
                    for message in self._build(batch, subscribe):
                        await socket.send(message)
                        await asyncio.sleep(self._send_interval_s)
                    if subscribe:
                        self._subscribed.update(batch)
                    else:
                        self._subscribed.difference_update(batch)

    @staticmethod
    async def _beat(socket, interval_s: float, message: str) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await socket.send(message)

    def _log_handler_error(self) -> None:
        now = time.monotonic()
        if now - self._last_error_log > 30:
            self._last_error_log = now
            log.exception("%s message handler failed", self.name)
