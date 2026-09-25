"""
VFP: Keeps the computer from sleeping while the terminal holds open positions, on every platform it runs on.
Changes when: the terminal runs on a new platform or needs a different wake policy.
Anti-goal:
1. Pretending to protect against a closed laptop lid, a forced restart or power loss — it cannot.
2. Being held for the whole process lifetime — only while pairs are open, so the machine can sleep otherwise.
3. A helper process that outlives the terminal: caffeinate is tied to this pid and dies with it, kill or not.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """
    Windows: SetThreadExecutionState on the calling thread. The flag lives as long as that thread,
    so hold() and release() must be called from the event loop thread.
    macOS: a `caffeinate -s -w <pid>` child; it blocks idle sleep and exits on its own when this process is gone.
    Other platforms: nothing to hold, said once in the log.
    """

    def __init__(self, platform: str = sys.platform, spawn=subprocess.Popen) -> None:
        self._platform = platform
        self._spawn = spawn
        self._held = False
        self._process = None
        self._warned = False

    @property
    def held(self) -> bool:
        return self._held

    def hold(self) -> None:
        if self._held:
            return
        if self._platform == "win32":
            self._held = self._windows(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        elif self._platform == "darwin":
            self._held = self._caffeinate()
        else:
            self._unsupported()
        if self._held:
            log.info("sleep blocked while positions are open")

    def release(self) -> None:
        if not self._held:
            return
        if self._platform == "win32":
            self._windows(ES_CONTINUOUS)
        else:
            self._stop_caffeinate()
        self._held = False
        log.info("sleep allowed again")

    def _windows(self, flags: int) -> bool:
        import ctypes

        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0

    def _caffeinate(self) -> bool:
        try:
            # -s: keep the system awake; -w: exit when this terminal's process does, so no helper is left behind.
            self._process = self._spawn(
                ["caffeinate", "-s", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            self._unsupported(f"caffeinate did not start: {exc}")
            return False

    def _stop_caffeinate(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
        except Exception as exc:
            log.warning("caffeinate did not stop: %s", exc)

    def _unsupported(self, detail: str = "") -> None:
        if self._warned:
            return
        self._warned = True
        log.warning("sleep cannot be blocked on this platform%s", f": {detail}" if detail else "")
