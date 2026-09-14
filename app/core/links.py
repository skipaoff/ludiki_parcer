"""
VFP: A browser link to the exchange's trading page for a contract.
Changes when: an exchange changes its trading page URL format.
Anti-goal:
1. Building links from the canonical token — multiplier contracts need the raw exchange symbol.

Ported from crypto_pars/price_gap.py::exchange_url and switched to raw symbols. Gate and Variational links checked 14.09.2026.
"""

from __future__ import annotations

from app.core.schemas import Instrument


def trade_url(instrument: Instrument) -> str | None:
    raw = instrument.symbol_raw.upper()
    if instrument.exchange == "binance":
        return f"https://www.binance.com/en/futures/{raw}"
    if instrument.exchange == "mexc":
        return f"https://futures.mexc.com/exchange/{raw}"
    if instrument.exchange == "gate":
        return f"https://www.gate.com/futures/USDT/{raw}"
    if instrument.exchange == "variational":
        return f"https://omni.variational.io/perpetual/{instrument.symbol_raw}"
    return None
