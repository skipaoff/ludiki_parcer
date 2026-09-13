"""
VFP: The asyncio event loop implementation the whole terminal runs on, chosen per platform.
Changes when: a dependency needs a different loop or a faster loop proves safe on a platform.
Anti-goal:
1. The selector loop on Windows — asyncpg's executemany on it silently dropped rows (13.09.2026: 9 of 15 runs of a
   1553-row upsert stored 1388 rows without any error; the proactor loop stored all rows in 15 of 15 runs).
2. Global event loop policies — deprecated since Python 3.14; the loop is passed to asyncio.run instead.

ccxt 4.5.78 no longer pulls in aiodns and aiohttp resolves names in threads, so the proactor loop serves ccxt,
aiohttp, websockets and uvicorn alike (checked on Windows 11 with live Binance and MEXC calls).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop
    try:
        import uvloop  # type: ignore[import-not-found]
    except ImportError:
        return asyncio.new_event_loop
    return uvloop.new_event_loop
