"""
VFP: Pushes live terminal state to every connected browser tab — snapshots several times a second, journal events immediately, heartbeats both ways.
Changes when: the live protocol between terminal and interface changes.
Anti-goal:
1. A slow or frozen tab slowing the terminal — each client has a bounded queue and is dropped when it overflows.
2. Building a snapshot per client — one snapshot per tick is shared by all clients.
3. Trading decisions here — the hub only transports state that other components computed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import orjson
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config.settings import UiSettings
from app.journal.journal import JournalEvent

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_QUEUE_LIMIT = 1000


def now_ms() -> int:
    return int(time.time() * 1000)


def encode(message: dict[str, Any]) -> str:
    return orjson.dumps(message, default=str).decode()


@dataclass(eq=False)
class _Client:
    websocket: WebSocket
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(CLIENT_QUEUE_LIMIT))
    last_seen: float = field(default_factory=time.monotonic)


class Hub:
    def __init__(self, snapshot: Callable[[], dict[str, Any]], settings: UiSettings) -> None:
        self._snapshot = snapshot
        self._settings = settings
        self._clients: set[_Client] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def push_event(self, event: JournalEvent) -> None:
        """Safe to call from any thread; delivery always happens on the event loop."""
        loop = self._loop
        if loop is None:
            return
        text = encode({"type": "event", "event": event.to_wire()})
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._broadcast(text)
        else:
            loop.call_soon_threadsafe(self._broadcast, text)

    async def serve(self, websocket: WebSocket) -> None:
        """Run one authenticated connection until it closes."""
        self._loop = asyncio.get_running_loop()
        client = _Client(websocket)
        self._clients.add(client)
        sender = asyncio.create_task(self._send_loop(client))
        try:
            client.queue.put_nowait(
                encode({"type": "hello", "protocol": PROTOCOL_VERSION, "server_ts_ms": now_ms(), "state": self._snapshot()})
            )
            while True:
                message = orjson.loads(await websocket.receive_text())
                client.last_seen = time.monotonic()
                if message.get("type") == "pong":
                    continue
        except (WebSocketDisconnect, orjson.JSONDecodeError, RuntimeError):
            pass
        finally:
            self._clients.discard(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    async def run(self) -> None:
        interval = 1 / self._settings.snapshot_hz
        next_ping = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            if not self._clients:
                continue
            try:
                self._broadcast(encode({"type": "snapshot", "server_ts_ms": now_ms(), "state": self._snapshot()}))
            except Exception:
                log.exception("snapshot failed")
            now = time.monotonic()
            if now >= next_ping:
                next_ping = now + self._settings.heartbeat_interval_s
                self._broadcast(encode({"type": "ping", "server_ts_ms": now_ms()}))
                for client in list(self._clients):
                    if now - client.last_seen > self._settings.heartbeat_timeout_s:
                        log.info("closing silent browser connection")
                        self._drop(client)

    async def close(self) -> None:
        for client in list(self._clients):
            self._drop(client)

    def _broadcast(self, text: str) -> None:
        for client in list(self._clients):
            try:
                client.queue.put_nowait(text)
            except asyncio.QueueFull:
                log.warning("browser connection too slow, dropping it")
                self._drop(client)

    def _drop(self, client: _Client) -> None:
        self._clients.discard(client)
        asyncio.get_running_loop().create_task(self._close_socket(client.websocket))

    @staticmethod
    async def _close_socket(websocket: WebSocket) -> None:
        with contextlib.suppress(Exception):
            await websocket.close(code=1001)

    @staticmethod
    async def _send_loop(client: _Client) -> None:
        while True:
            await client.websocket.send_text(await client.queue.get())
