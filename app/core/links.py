"""
VFP: A browser link to the exchange's trading page for a contract.
Changes when: an exchange changes its trading page URL format.
Anti-goal:
1. Building links from the canonical token — multiplier contracts need the raw exchange symbol.

Ported from crypto_pars/price_gap.py::exchange_url and switched to raw symbols. Gate, Aster, BingX and Variational links checked 14.09.2026; Bybit, Bitget, KuCoin and Hyperliquid 15.09.2026.
"""

from __future__ import annotations

from app.core.schemas import Instrument


def trade_url(instrument: Instrument) -> str | None:
    raw = instrument.symbol_raw.upper()
    if instrument.exchange == "binance":
        return f"https://www.binance.com/en/futures/{raw}"
    if instrument.exchange == "mexc":
        # futures.mexc.com/exchange/<symbol> was retired: it now redirects to plain http on www, where Akamai
        # answers "Access Denied", and it loses the symbol on the way. Checked 22.09.2026.
        return f"https://www.mexc.com/futures/{raw}"
    if instrument.exchange == "gate":
        return f"https://www.gate.com/futures/USDT/{raw}"
    if instrument.exchange == "aster":
        return f"https://www.asterdex.com/en/trade/pro/futures/{raw}"
    if instrument.exchange == "bingx":
        return f"https://bingx.com/en/perpetual/{raw}/"
    if instrument.exchange == "bybit":
        return f"https://www.bybit.com/trade/usdt/{raw}"
    if instrument.exchange == "bitget":
        return f"https://www.bitget.com/futures/usdt/{raw}"
    if instrument.exchange == "kucoin":
        return f"https://www.kucoin.com/trade/futures/{raw}"
    if instrument.exchange == "hyperliquid":
        return f"https://app.hyperliquid.xyz/trade/{instrument.symbol_raw}"
    if instrument.exchange == "variational":
        return f"https://omni.variational.io/perpetual/{instrument.symbol_raw}"
    return None
