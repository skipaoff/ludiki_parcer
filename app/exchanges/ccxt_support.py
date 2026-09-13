"""
VFP: Shared plumbing for adapters built on ccxt — timed calls and fault-tolerant check steps.
Changes when: every ccxt-based adapter needs the same new helper.
Anti-goal:
1. Exchange-specific parsing here — that lives next to each adapter.
2. Error text carrying signatures or keys to callers — messages are cut and pass through the redactor upstream.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, TypeVar

MAX_ERROR_LENGTH = 300
T = TypeVar("T")


def now_ms() -> float:
    return time.time() * 1000


def describe_error(step: str, exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    if len(text) > MAX_ERROR_LENGTH:
        text = text[:MAX_ERROR_LENGTH] + "…"
    return f"{step}: {type(exc).__name__}: {text}"


async def step(errors: list[str], name: str, call: Awaitable[Any]) -> Any:
    """Await one check step; on failure record it and return None so the other steps still run."""
    try:
        return await call
    except Exception as exc:
        errors.append(describe_error(name, exc))
        return None


def parse(errors: list[str], name: str, parser: Callable[[Any], T], raw: Any) -> T | None:
    """Run a parser on a raw answer; a malformed answer is recorded like a failed step instead of raising."""
    if raw is None:
        return None
    try:
        return parser(raw)
    except Exception as exc:
        errors.append(describe_error(name, exc))
        return None


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None
