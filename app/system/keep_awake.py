"""
VFP: Keeps the computer from sleeping while the terminal holds open positions.
Changes when: the terminal runs on a new platform or needs a different wake policy.
Anti-goal:
1. Pretending to protect against a closed laptop lid, a forced Windows Update restart or power loss — it cannot.
2. Being held for the whole process lifetime — only while pairs are open, so the machine can sleep otherwise.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """
    Windows: SetThreadExecutionState on the calling thread. The flag lives as long as that thread,
    so hold() and release() must be called from the event loop thread.
    """

    def __init__(self) -> None:
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def hold(self) -> None:
        if self._held:
            return
        if self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            self._held = True
            log.info("sleep blocked while positions are open")

    def release(self) -> None:
        if not self._held:
            return
        self._set(ES_CONTINUOUS)
        self._held = False
        log.info("sleep allowed again")

    @staticmethod
    def _set(flags: int) -> bool:
        if sys.platform != "win32":
            log.warning("keep-awake is implemented for Windows only")
            return False
        import ctypes

        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
