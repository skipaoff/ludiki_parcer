"""
VFP: Splits a migration file into single SQL statements, the way psql sends them.
Changes when: migrations start using syntax the splitter does not understand.
Anti-goal:
1. Sending a whole file as one query — PostgreSQL runs it as one implicit transaction, and TimescaleDB
   continuous aggregates refuse to be created inside a transaction block.
2. Splitting inside string literals, quoted identifiers, dollar-quoted bodies or comments.
"""

from __future__ import annotations

import re

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def split_statements(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    i = 0
    length = len(script)

    def flush() -> None:
        text = "".join(current).strip()
        if _has_code(text):
            statements.append(text)
        current.clear()

    while i < length:
        char = script[i]
        pair = script[i : i + 2]

        if pair == "--":
            end = script.find("\n", i)
            end = length if end == -1 else end
            current.append(script[i:end])
            i = end
        elif pair == "/*":
            end = script.find("*/", i + 2)
            end = length if end == -1 else end + 2
            current.append(script[i:end])
            i = end
        elif char in ("'", '"'):
            end = _quoted_end(script, i, char)
            current.append(script[i:end])
            i = end
        elif char == "$" and (match := _DOLLAR_TAG.match(script, i)):
            tag = match.group(0)
            end = script.find(tag, match.end())
            end = length if end == -1 else end + len(tag)
            current.append(script[i:end])
            i = end
        elif char == ";":
            flush()
            i += 1
        else:
            current.append(char)
            i += 1

    flush()
    return statements


def _quoted_end(script: str, start: int, quote: str) -> int:
    """Index just past the closing quote; a doubled quote is an escaped quote."""
    i = start + 1
    while i < len(script):
        if script[i] == quote:
            if i + 1 < len(script) and script[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return len(script)


def _has_code(text: str) -> bool:
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)
    return bool(without_comments.strip())
