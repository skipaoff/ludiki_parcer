"""
VFP: Process-wide logging to console and a rotating file, with every known secret value masked before it is written.
Changes when: log destinations, formats or the set of redaction rules change.
Anti-goal:
1. A secret reaching any handler — redaction runs on the final message, after argument formatting.
2. Business events in these logs — the trading journal goes to the `events` table through the journal.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

MASK = "***"
MIN_SECRET_LENGTH = 6
# Header and query names that carry credentials or signatures in exchange requests.
SENSITIVE_FIELDS = re.compile(
    r"(?i)\b(x-mbx-apikey|apikey|api_key|api-key|secret|signature|password|token|listenkey)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',&}]+)"
)


class SecretRedactor:
    """Masks registered secret values and credential-looking fields in text. Thread-safe."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def add(self, value: str | None) -> None:
        if value and len(value) >= MIN_SECRET_LENGTH:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, MASK)
        return SENSITIVE_FIELDS.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor, fmt: str) -> None:
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(logs_dir: Path, level: str, redactor: SecretRedactor) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter(redactor, LOG_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    file = RotatingFileHandler(logs_dir / "terminal.log", maxBytes=20_000_000, backupCount=10, encoding="utf-8")
    file.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file)
    root.setLevel(level.upper())
    # Access logs would print every request line; the terminal has nothing to learn from them.
    logging.getLogger("uvicorn.access").disabled = True
