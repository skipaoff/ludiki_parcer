"""
VFP: Makes every TLS connection of the process verify certificates against the operating system's trust store, through one shared SSL context.
Changes when: the terminal stops needing the OS store or a library gains its own way to use it.
Anti-goal:
1. Disabling verification — certificates are still checked, only the source of trusted roots changes.
2. Late activation — it must run before any library builds its SSL context.
3. A context per connection — building one blocks the event loop for 0.1 s (0.5 s with certifi's bundle, as ccxt does)
   on Windows; with a dozen exchange clients and websocket reconnects that stalled the loop for seconds (15.09.2026).

Why: ccxt pins certifi's bundle, while antivirus HTTPS inspection (Avast on the dev machine) re-signs traffic with a
root that only the Windows store trusts. Exclusions in the antivirus remain the proper fix for exchange traffic.
"""

from __future__ import annotations

import ssl
from typing import TypeVar

import truststore

_active = False
_context: ssl.SSLContext | None = None
Client = TypeVar("Client")


def use_system_trust_store() -> None:
    global _active
    if not _active:
        truststore.inject_into_ssl()
        _active = True


def shared_context() -> ssl.SSLContext:
    global _context
    if _context is None:
        _context = ssl.create_default_context()
    return _context


def with_shared_context(client: Client) -> Client:
    """A ccxt client that reuses the process context instead of building its own on the first request."""
    client.ssl_context = shared_context()  # type: ignore[attr-defined]
    return client
