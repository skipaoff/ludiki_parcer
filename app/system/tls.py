"""
VFP: Makes every TLS connection of the process verify certificates against the operating system's trust store.
Changes when: the terminal stops needing the OS store or a library gains its own way to use it.
Anti-goal:
1. Disabling verification — certificates are still checked, only the source of trusted roots changes.
2. Late activation — it must run before any library builds its SSL context.

Why: ccxt pins certifi's bundle, while antivirus HTTPS inspection (Avast on the dev machine) re-signs traffic with a
root that only the Windows store trusts. Exclusions in the antivirus remain the proper fix for exchange traffic.
"""

from __future__ import annotations

import truststore

_active = False


def use_system_trust_store() -> None:
    global _active
    if not _active:
        truststore.inject_into_ssl()
        _active = True
