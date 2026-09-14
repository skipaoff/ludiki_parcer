"""
VFP: Variational Omni prices into MarketState — best quotes, mark and volume of every listing, and a two-level book per listing built from its $1k and $100k quotes.
Changes when: Variational changes its statistics or starts streaming prices.
Anti-goal:
1. A quote looking fresher than it is — books and best prices carry Variational's own updated_at, so a quote cached on
   their side ages like any other stale data.
2. Requests of its own — the adapter's cached statistics are shared with the catalog and the link probe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Iterable

from app.exchanges.variational.adapter import VariationalAdapter, parse_tiered
from app.market.state import MarketState

log = logging.getLogger(__name__)

EXCHANGE = "variational"


def apply_stats(state: MarketState, stats: dict[str, Any], now_ms: float) -> int:
    applied = 0
    for quote in parse_tiered(stats):
        # A timestamp from the future (clock drift) is clamped to now; an old one keeps its age.
        seen_ms = min(float(quote.updated_ms), now_ms) if quote.updated_ms else now_ms
        if quote.best_bid and quote.best_ask:
            state.set_top(EXCHANGE, quote.ticker, quote.best_bid, quote.best_ask, quote.updated_ms, received_ms=seen_ms)
        state.set_mark(EXCHANGE, quote.ticker, quote.mark, None, quote.volume24h_usd)
        if quote.bids and quote.asks:
            state.set_book(EXCHANGE, quote.ticker, quote.bids, quote.asks, quote.updated_ms, received_ms=seen_ms)
            applied += 1
    return applied


class VariationalMarket:
    """Books for every listing come with each poll, so subscriptions are no-ops."""

    def __init__(self, state: MarketState, adapter: Callable[[], VariationalAdapter], poll_ms: int) -> None:
        self._state = state
        self._adapter = adapter
        self._poll_s = poll_ms / 1000
        self.polls = 0
        self.poll_errors = 0
        self.listings = 0
        self._last_error_log = 0.0

    def set_depth(self, symbols: Iterable[str]) -> None:
        return None

    def resubscribe_depth(self, symbol: str) -> None:
        return None

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                stats = await self._adapter().stats()
                self.listings = apply_stats(self._state, stats, time.time() * 1000)
                self._state.count(EXCHANGE)
                self.polls += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.poll_errors += 1
                if time.monotonic() - self._last_error_log > 30:
                    self._last_error_log = time.monotonic()
                    log.warning("variational statistics poll failed: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(max(0.0, self._poll_s - (time.monotonic() - started)))

    def stats(self) -> dict[str, Any]:
        return {"polls": self.polls, "poll_errors": self.poll_errors, "listings": self.listings}
