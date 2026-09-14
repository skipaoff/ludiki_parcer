"""
VFP: Private account streams of Binance and MEXC used as a doorbell — an order or position change wakes whoever waits for it, so status checks and position polls happen at once instead of on the next timer.
Changes when: the private stream protocols change or more private events become useful.
Anti-goal:
1. Being the source of truth — every event only triggers a REST check; if a stream is silent or broken, timers still work.
2. Keys or signatures in logs or events.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Callable

import orjson
import websockets

log = logging.getLogger(__name__)

BINANCE_WS = "wss://fstream.binance.com/ws/"
MEXC_WS = "wss://contract.mexc.com/edge"
LISTEN_KEY_KEEPALIVE_S = 30 * 60
RECONNECT_MAX_S = 60


class OrderEvents:
    """Waiters for order ids; a stream event for an id wakes its waiter early."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Event] = {}
        self.received = 0

    async def wait(self, client_order_id: str, timeout_s: float) -> bool:
        event = self._waiters.setdefault(client_order_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
            return True
        except TimeoutError:
            return False
        finally:
            self._waiters.pop(client_order_id, None)

    def notify(self, client_order_id: str) -> None:
        self.received += 1
        event = self._waiters.get(client_order_id)
        if event is not None:
            event.set()


def binance_event(message: dict[str, Any]) -> tuple[str, str | None]:
    """(kind, client order id) from a Binance futures user data event."""
    kind = message.get("e", "")
    if kind == "ORDER_TRADE_UPDATE":
        return "order", (message.get("o") or {}).get("c")
    if kind == "ACCOUNT_UPDATE":
        return "account", None
    if kind == "listenKeyExpired":
        return "expired", None
    return "other", None


def mexc_event(message: dict[str, Any]) -> tuple[str, str | None]:
    channel = message.get("channel", "")
    if channel == "push.personal.order":
        return "order", (message.get("data") or {}).get("externalOid")
    if channel in ("push.personal.position", "push.personal.order.deal", "push.personal.asset"):
        return "account", None
    if channel == "rs.login":
        return "login", str(message.get("data"))
    return "other", None


def mexc_login_message(api_key: str, secret: str, now_ms: int) -> str:
    request_time = str(now_ms)
    signature = hmac.new(secret.encode(), (api_key + request_time).encode(), hashlib.sha256).hexdigest()
    return orjson.dumps({"method": "login", "param": {"apiKey": api_key, "reqTime": request_time, "signature": signature}}).decode()


class PrivateStreams:
    def __init__(
        self,
        adapter: Callable[[str], Any],
        orders: OrderEvents,
        on_account_change: Callable[[], None],
        enabled_exchanges: Callable[[], list[str]],
    ) -> None:
        """adapter(exchange) gives the current adapter; keys stay inside it (listen key calls, login message)."""
        self._adapter = adapter
        self._orders = orders
        self._on_account_change = on_account_change
        self._enabled = enabled_exchanges
        self.connected: dict[str, bool] = {"binance": False, "mexc": False}

    async def run(self) -> None:
        await asyncio.gather(self._loop("binance", self._binance_once), self._loop("mexc", self._mexc_once))

    async def _loop(self, exchange: str, once: Callable[[], Any]) -> None:
        backoff = 5.0
        while True:
            if exchange not in self._enabled():
                self.connected[exchange] = False
                await asyncio.sleep(10)
                continue
            try:
                await once()
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s private stream: %s: %s", exchange, type(exc).__name__, str(exc)[:200])
            self.connected[exchange] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    def _dispatch(self, kind: str, order_id: str | None) -> None:
        if kind == "order" and order_id:
            self._orders.notify(order_id)
            self._on_account_change()
        elif kind == "account":
            self._on_account_change()

    async def _binance_once(self) -> None:
        adapter = self._adapter("binance")
        listen_key = await adapter.create_listen_key()
        async with websockets.connect(BINANCE_WS + listen_key, open_timeout=10, close_timeout=2, max_size=2**22) as socket:
            self.connected["binance"] = True
            keepalive = asyncio.create_task(self._binance_keepalive(adapter))
            try:
                async for raw in socket:
                    kind, order_id = binance_event(orjson.loads(raw))
                    if kind == "expired":
                        return
                    self._dispatch(kind, order_id)
            finally:
                keepalive.cancel()

    @staticmethod
    async def _binance_keepalive(adapter: Any) -> None:
        while True:
            await asyncio.sleep(LISTEN_KEY_KEEPALIVE_S)
            await adapter.keepalive_listen_key()

    async def _mexc_once(self) -> None:
        adapter = self._adapter("mexc")
        async with websockets.connect(MEXC_WS, open_timeout=10, close_timeout=2, max_size=2**22) as socket:
            await socket.send(adapter.login_message(int(time.time() * 1000)))
            pinger = asyncio.create_task(self._mexc_ping(socket))
            try:
                async for raw in socket:
                    kind, detail = mexc_event(orjson.loads(raw))
                    if kind == "login":
                        self.connected["mexc"] = detail == "success"
                        if detail != "success":
                            raise ConnectionError(f"mexc login refused: {detail}")
                        continue
                    self._dispatch(kind, detail)
            finally:
                pinger.cancel()

    @staticmethod
    async def _mexc_ping(socket: Any) -> None:
        while True:
            await asyncio.sleep(15)
            await socket.send('{"method":"ping"}')
