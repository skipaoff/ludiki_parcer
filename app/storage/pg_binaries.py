"""
VFP: The path to pg_ctl on this machine, wherever the platform keeps PostgreSQL.
Changes when: a supported platform stores PostgreSQL somewhere new.
Anti-goal:
1. Touching the filesystem or the environment itself — the lookups arrive as arguments, so this stays testable.
2. A Windows-shaped default that silently means "no cluster" on a Mac: the same config.toml must work on both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

# Homebrew keeps versioned formulae out of PATH, so the two prefixes it uses are worth looking into by name.
HOMEBREW_BIN_DIRS = ("/opt/homebrew/opt/postgresql@18/bin", "/usr/local/opt/postgresql@18/bin")


def find_pg_ctl(
    configured_bin_dir: Path,
    *,
    platform: str,
    exists: Callable[[Path], bool],
    which: Callable[[str], str | None],
) -> Path | None:
    """
    Search order: the configured directory, then PATH, then the places a package manager uses on this platform.
    Returns None when PostgreSQL is not installed here — the caller then leaves the cluster alone.
    """
    for candidate in (configured_bin_dir / "pg_ctl.exe", configured_bin_dir / "pg_ctl"):
        if exists(candidate):
            return candidate
    found = which("pg_ctl")
    if found:
        return Path(found)
    if platform == "darwin":
        for directory in HOMEBREW_BIN_DIRS:
            candidate = Path(directory) / "pg_ctl"
            if exists(candidate):
                return candidate
    return None
