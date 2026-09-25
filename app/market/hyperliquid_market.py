"""
VFP: Hyperliquid market data into MarketState — best prices, mark, oracle and volume of every coin from one info request, order books of candidates from the fast l2Book websocket channel.
Changes when: Hyperliquid changes its info answers or websocket channels.
Anti-goal:
1. The plain l2Book channel — it pushed a book only every 5.4 s; with "fast": true it pushes every ~0.5 s (checked live 15.09.2026).
2. Streaming best prices of every coin — bbo sends about 7 messages a second per coin; the radar uses impactPxs from
   metaAndAssetCtxs instead, polled every 2 s (weight 20 of 1,200 a minute per IP).
3. A silent socket — Hyperliquid closes connections idle for 60 s.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import aiohttp
import orjson

from app.system.tls import client_session
from app.market.depth_pool import DepthPool, Poller
from app.market.state import MarketState

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
COINS_PER_CONNECTION = 100
EXCHANGE = "hyperliquid"
PING = orjson.dumps({"method": "ping"}).decode()


def depth_messages(coins: list[str], subscribe: bool) -> Iterable[str]:
    for coin in coins:
        yield orjson.dumps(
            {"method": "subscribe" if subscribe else "unsubscribe", "subscription": {"type": "l2Book", "coin": coin, "fast": True}}
        ).decode()


def handle_frame(state: MarketState, raw: str | bytes) -> None:
    message: dict[str, Any] = orjson.loads(raw)
    if message.get("channel") != "l2Book":
        return
    data = message.get("data") or {}
    levels = data.get("levels") or [[], []]
    state.count(EXCHANGE)
    state.set_book(
        EXCHANGE,
        data["coin"],
        [[level["px"], level["sz"]] for level in levels[0]],
        [[level["px"], level["sz"]] for level in levels[1]],
        int(data.get("time") or 0),
    )


def context_rows(answer: list[Any]) -> list[tuple[str, float, float, float | None, float | None, float | None]]:
    """metaAndAssetCtxs — (coin, impact bid, impact ask, markPx, oraclePx, dayNtlVlm); coins without impact prices get 0."""
    meta, contexts = answer[0], answer[1]
    rows = []
    for coin, context in zip(meta.get("universe") or [], contexts):
        impact = context.get("impactPxs") or [0, 0]
        number = lambda key: float(context[key]) if context.get(key) else None  # noqa: E731
        rows.append((coin["name"], float(impact[0] or 0), float(impact[1] or 0), number("markPx"), number("oraclePx"), number("dayNtlVlm")))
    return rows


class HyperliquidMarket:
    def __init__(self, state: MarketState, poll_ms: int) -> None:
        self._state = state
        self._pool = DepthPool(
            EXCHANGE, WS_URL, lambda raw: handle_frame(self._state, raw), depth_messages,
            per_connection=COINS_PER_CONNECTION, batch_size=1, send_interval_s=0.05, heartbeat=(30, PING),
        )
        self._poller = Poller("hyperliquid contexts", poll_ms / 1000, self._poll)
        self._session: aiohttp.ClientSession | None = None

    def set_depth(self, coins: Iterable[str]) -> None:
        self._pool.set_depth(coins)

    def resubscribe_depth(self, coin: str) -> None:
        self._pool.resubscribe(coin)

    async def _poll(self) -> None:
        assert self._session is not None
        async with self._session.post(INFO_URL, json={"type": "metaAndAssetCtxs"}) as response:
            response.raise_for_status()
            answer = orjson.loads(await response.read())
        now = int(time.time() * 1000)
        for coin, bid, ask, mark, index, volume in context_rows(answer):
            if bid > 0 and ask > 0:
                self._state.set_top(EXCHANGE, coin, bid, ask, now)
            self._state.set_mark(EXCHANGE, coin, mark, index, volume)
        self._state.count(EXCHANGE)

    async def run(self) -> None:
        self._pool.start()
        try:
            async with client_session(timeout=aiohttp.ClientTimeout(total=10)) as session:
                self._session = session
                await self._poller.run()
        finally:
            self._pool.stop()

    def stats(self) -> dict[str, Any]:
        return {**self._pool.stats(), "polls": self._poller.polls, "poll_errors": self._poller.errors}
