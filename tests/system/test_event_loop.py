import asyncio
import sys

import pytest

from app.system.event_loop import loop_factory


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific loop choice")
def test_windows_never_runs_on_the_selector_loop():
    loop = loop_factory()()
    try:
        assert not isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
