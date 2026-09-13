"""
VFP: The asyncio event loop implementation the whole terminal runs on, chosen per platform.
Changes when: a dependency needs a different loop or a faster loop becomes available on a platform.
Anti-goal:
1. Proactor loop on Windows — aiodns (pulled in by ccxt) refuses to run on it.
2. Global event loop policies — deprecated since Python 3.14; the loop is passed to asyncio.run instead.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    if sys.platform == "win32":
        # Selector loop on Windows is limited to 512 sockets; the terminal needs a few dozen.
        return asyncio.SelectorEventLoop
    try:
        import uvloop  # type: ignore[import-not-found]
    except ImportError:
        return asyncio.new_event_loop
    return uvloop.new_event_loop
