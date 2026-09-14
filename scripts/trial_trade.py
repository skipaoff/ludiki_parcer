"""
VFP: The stage 1 trial trade — opens and immediately closes the smallest position on one exchange through the terminal's own adapter, and prints everything the exchange answered.
Changes when: the adapter trading contract changes or the trial needs to prove something new.
Anti-goal:
1. Running by accident — it asks for the exchange name to be typed back, and refuses without --i-understand.
2. Anything beyond one open and one reduce-only close of the minimum quantity.
3. Printing keys — keys are read from Windows Credential Manager and never shown.

Usage (from the repository folder):
    .venv\\Scripts\\python.exe scripts\\trial_trade.py binance --demo --i-understand
    .venv\\Scripts\\python.exe scripts\\trial_trade.py mexc --symbol DOGE_USDT --i-understand
    .venv\\Scripts\\python.exe scripts\\trial_trade.py gate --i-understand
Results go to ../ludik-data/trials/<exchange>-<time>.json; copy the findings to docs/EXCHANGES.md as [проверено].
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import BinanceSettings, ExchangesSettings, load_settings  # noqa: E402
from app.core.schemas import LegSide  # noqa: E402
from app.exchanges.service import default_adapter_factory  # noqa: E402
from app.keystore.keystore import Keystore, api_key_name, api_secret_name  # noqa: E402
from app.system.event_loop import loop_factory  # noqa: E402
from app.system.log_setup import SecretRedactor  # noqa: E402
from app.system.tls import use_system_trust_store  # noqa: E402

DEFAULT_SYMBOLS = {"binance": "DOGEUSDT", "mexc": "DOGE_USDT", "gate": "DOGE_USDT"}


def plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


async def trial(exchange: str, symbol: str, demo: bool) -> dict:
    settings = ExchangesSettings(binance=BinanceSettings(demo=demo))
    redactor = SecretRedactor()
    keystore = Keystore(redactor)
    slot = f"{exchange}-demo" if exchange == "binance" and demo else exchange
    key, secret = keystore.get(api_key_name(slot)), keystore.get(api_secret_name(slot))
    if not key or not secret:
        raise SystemExit(f"no keys for {slot}: add them in Settings → Exchanges first")
    adapter = default_adapter_factory(exchange, key, secret, settings)
    report: dict = {"exchange": exchange, "symbol": symbol, "demo": demo, "started_ms": int(time.time() * 1000), "steps": []}
    try:
        facts = await adapter.check_account()
        report["steps"].append({"account_check": plain(asdict(facts))})
        instruments = {item.symbol_raw: item for item in await adapter.load_instruments()}
        instrument = instruments.get(symbol)
        if instrument is None:
            raise SystemExit(f"{symbol} is not a tradable USDT perpetual on {exchange}")
        report["steps"].append({"instrument": plain(asdict(instrument))})
        quotes = await adapter.fetch_quotes()
        price = quotes[symbol].ask or quotes[symbol].mark
        # The smallest quantity the exchange accepts that also clears the minimum notional, with a 20 % margin.
        units = instrument.min_qty_units
        while units * instrument.qty_unit_tokens * price / instrument.price_unit_tokens < instrument.min_notional_usd * Decimal("1.2"):
            units += instrument.qty_step_units
        report["steps"].append({"plan": {"qty_units": str(units), "qty_tokens": str(units * instrument.qty_unit_tokens), "price": str(price)}})
        print(f"about to BUY {units} units of {symbol} at market on {exchange}{' (demo)' if demo else ''}, ~${units * instrument.qty_unit_tokens * price / instrument.price_unit_tokens:.2f}")
        if input(f"type {exchange} to send the order: ").strip() != exchange:
            raise SystemExit("cancelled, nothing was sent")

        await adapter.prepare_symbol(instrument, 1, True)
        report["steps"].append({"prepare_symbol": "ok"})
        tag = hex(int(time.time()))[2:]
        opened = await adapter.place_market_order(instrument, LegSide.LONG, True, units, f"lktrial{tag}o", 1, True)
        report["steps"].append({"open_order": plain(asdict(opened))})
        positions = await adapter.fetch_positions(instruments)
        report["steps"].append({"positions_after_open": [plain(asdict(p)) for p in positions if p.symbol_raw == symbol]})
        closed = await adapter.place_market_order(instrument, LegSide.LONG, False, units, f"lktrial{tag}c", 1, True)
        report["steps"].append({"close_order": plain(asdict(closed))})
        status = await adapter.fetch_order(instrument, f"lktrial{tag}o", units * instrument.qty_unit_tokens)
        report["steps"].append({"open_order_status": plain(asdict(status))})
        for name, order in (("open_fills", opened), ("close_fills", closed)):
            report["steps"].append({name: [plain(asdict(fill)) for fill in await adapter.fetch_fills(instrument, order)]})
        positions = await adapter.fetch_positions(instruments)
        report["steps"].append({"positions_after_close": [plain(asdict(p)) for p in positions if p.symbol_raw == symbol]})
        report["steps"].append({"balance": plain(asdict(await adapter.fetch_balance()))})
    finally:
        await adapter.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Open and close the minimum position once, to see what the exchange really answers.")
    parser.add_argument("exchange", choices=["binance", "mexc", "gate"])
    parser.add_argument("--symbol")
    parser.add_argument("--demo", action="store_true", help="Binance demo trading with demo keys")
    parser.add_argument("--i-understand", action="store_true", help="real orders are sent (on a live account, real money)")
    args = parser.parse_args()
    if not args.i_understand:
        print("refusing to run without --i-understand: this sends real market orders")
        return 2
    use_system_trust_store()
    load_settings()
    report = asyncio.run(trial(args.exchange, args.symbol or DEFAULT_SYMBOLS[args.exchange], args.demo), loop_factory=loop_factory())
    folder = Path(__file__).resolve().parents[2] / "ludik-data" / "trials"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{args.exchange}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
