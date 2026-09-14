"""
VFP: The terminal's view of each connected exchange — link health, clock drift, stored keys and the verdict of the last account check.
Changes when: the terminal learns a new exchange-level state or action (keys, checks, links).
Anti-goal:
1. A raw key or secret leaving this service — callers see masked keys and verdicts only.
2. Flapping link alarms — a link goes down only after consecutive failed probes.
3. A check result for keys that were replaced while it ran.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from app.config.settings import READ_ONLY_EXCHANGES, ExchangesSettings
from app.core.account import AccountFacts, judge
from app.exchanges.base import ExchangeAdapter
from app.exchanges.binance.adapter import BinanceAdapter
from app.exchanges.ccxt_support import describe_error
from app.exchanges.gate.adapter import GateAdapter
from app.exchanges.mexc.adapter import MexcAdapter
from app.exchanges.variational.adapter import VariationalAdapter
from app.journal.journal import Journal, Level
from app.keystore.keystore import Keystore, api_key_name, api_secret_name, mask
from app.system.log_setup import SecretRedactor

log = logging.getLogger(__name__)

FAILURES_BEFORE_DOWN = 2
KEY_PATTERN = re.compile(r"^[\x21-\x7e]{8,256}$")

AdapterFactory = Callable[[str, str | None, str | None, ExchangesSettings], ExchangeAdapter]


class InvalidKeys(ValueError):
    pass


class UnknownExchange(KeyError):
    pass


def default_adapter_factory(name: str, key: str | None, secret: str | None, settings: ExchangesSettings) -> ExchangeAdapter:
    if name == "binance":
        return BinanceAdapter(key, secret, demo=settings.binance.demo, timeout_s=settings.request_timeout_s)
    if name == "mexc":
        return MexcAdapter(key, secret, timeout_s=settings.request_timeout_s)
    if name == "gate":
        return GateAdapter(key, secret, timeout_s=settings.request_timeout_s)
    if name == "variational":
        return VariationalAdapter(timeout_s=settings.request_timeout_s, min_interval_ms=settings.variational.poll_ms)
    raise UnknownExchange(name)


@dataclass
class _Exchange:
    name: str
    demo: bool
    adapter: ExchangeAdapter
    generation: int = 0  # bumps on every key change, so a check started with old keys cannot land
    link: str = "unknown"  # unknown | up | down
    failures: int = 0
    ping_ms: int | None = None
    clock_offset_ms: int | None = None
    probe_error: str | None = None
    key_masked: str | None = None
    checking: bool = False
    check: dict[str, Any] | None = None


def _jsonable(facts: AccountFacts) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value

    return convert(asdict(facts))


class ExchangeService:
    def __init__(
        self,
        settings: ExchangesSettings,
        keystore: Keystore,
        journal: Journal,
        submit_row: Callable[[str, dict[str, Any]], None],
        redactor: SecretRedactor,
        adapter_factory: AdapterFactory = default_adapter_factory,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._keystore = keystore
        self._journal = journal
        self._submit_row = submit_row
        self._redactor = redactor
        self._factory = adapter_factory
        self._clock = clock
        self._exchanges: dict[str, _Exchange] = {}
        for name in settings.enabled_names():
            key, secret = (None, None) if name in READ_ONLY_EXCHANGES else self._read_keys(name)
            self._exchanges[name] = _Exchange(
                name=name,
                demo=name == "binance" and settings.binance.demo,
                adapter=adapter_factory(name, key, secret, settings),
                key_masked=mask(key) if key and secret else None,
            )

    @property
    def names(self) -> list[str]:
        return list(self._exchanges)

    @staticmethod
    def read_only(name: str) -> bool:
        return name in READ_ONLY_EXCHANGES

    # ── keys ─────────────────────────────────────────────────────────────────

    def _slot(self, name: str) -> str:
        """Demo keys live apart from live keys, so switching modes never mixes them up."""
        return f"{name}-demo" if name == "binance" and self._settings.binance.demo else name

    def _read_keys(self, name: str) -> tuple[str | None, str | None]:
        slot = self._slot(name)
        return self._keystore.get(api_key_name(slot)), self._keystore.get(api_secret_name(slot))

    def _get(self, name: str) -> _Exchange:
        exchange = self._exchanges.get(name)
        if exchange is None:
            raise UnknownExchange(name)
        return exchange

    def account_taker_fee_pct(self, name: str) -> Decimal | None:
        """Taker fee from the last accepted account check, None until there is one."""
        exchange = self._exchanges.get(name)
        if exchange is None or exchange.check is None or not exchange.check["accepted"]:
            return None
        value = exchange.check["facts"].get("taker_fee_pct")
        return None if value is None else Decimal(value)

    def adapter(self, name: str) -> ExchangeAdapter:
        """The current client of an exchange; it changes when keys are replaced, so do not keep it."""
        return self._get(name).adapter

    async def save_keys(self, name: str, api_key: str, api_secret: str) -> dict[str, Any]:
        exchange = self._get(name)
        if self.read_only(name):
            raise InvalidKeys(f"{name} has no trading API, keys are not used")
        api_key, api_secret = api_key.strip(), api_secret.strip()
        if not KEY_PATTERN.match(api_key) or not KEY_PATTERN.match(api_secret):
            raise InvalidKeys("key and secret must be 8–256 visible ASCII characters without spaces")
        slot = self._slot(name)
        self._keystore.set(api_key_name(slot), api_key)
        self._keystore.set(api_secret_name(slot), api_secret)
        await self._replace_adapter(exchange, api_key, api_secret)
        exchange.key_masked = mask(api_key)
        exchange.check = None
        self._journal.emit(Level.INFO, "exchange", "keys_saved", exchange=name, key=exchange.key_masked)
        return self.describe_one(name)

    async def delete_keys(self, name: str) -> dict[str, Any]:
        exchange = self._get(name)
        slot = self._slot(name)
        self._keystore.delete(api_key_name(slot))
        self._keystore.delete(api_secret_name(slot))
        await self._replace_adapter(exchange, None, None)
        exchange.key_masked = None
        exchange.check = None
        self._journal.emit(Level.INFO, "exchange", "keys_deleted", exchange=name)
        return self.describe_one(name)

    async def _replace_adapter(self, exchange: _Exchange, key: str | None, secret: str | None) -> None:
        old = exchange.adapter
        exchange.adapter = self._factory(exchange.name, key, secret, self._settings)
        exchange.generation += 1
        try:
            # A request still running on the old client fails once; the link needs two failures to go down.
            await old.close()
        except Exception as exc:
            log.warning("closing old %s client failed: %s", exchange.name, exc)

    # ── account check ────────────────────────────────────────────────────────

    async def check(self, name: str) -> dict[str, Any]:
        exchange = self._get(name)
        if self.read_only(name):
            raise InvalidKeys(f"{name} has no trading API, there is no account to check")
        if exchange.key_masked is None:
            raise InvalidKeys("no keys saved for this exchange")
        if exchange.checking:
            return self.describe_one(name)
        exchange.checking = True
        generation = exchange.generation
        try:
            facts = await asyncio.wait_for(exchange.adapter.check_account(), self._settings.request_timeout_s * 4)
        except Exception as exc:
            facts = AccountFacts(errors=(describe_error("check", exc),))
        finally:
            exchange.checking = False
        if generation != exchange.generation:
            return self.describe_one(name)

        facts = self._redact_facts(facts)
        verdict = judge(facts, self._settings.clock_warning_ms)
        exchange.check = {
            "checked_at_ms": int(self._clock() * 1000),
            "facts": _jsonable(facts),
            "blocking": list(verdict.blocking),
            "warnings": list(verdict.warnings),
            "accepted": verdict.accepted,
        }
        level = Level.INFO if verdict.accepted and not verdict.warnings else Level.WARNING
        self._journal.emit(
            level,
            "exchange",
            "check_ok" if verdict.accepted else "check_rejected",
            exchange=name,
            blocking=list(verdict.blocking),
            warnings=list(verdict.warnings),
            errors=list(facts.errors),
        )
        return self.describe_one(name)

    async def check_all_with_keys(self) -> None:
        await asyncio.gather(
            *(self.check(name) for name, exchange in self._exchanges.items() if exchange.key_masked),
            return_exceptions=True,
        )

    def _redact_facts(self, facts: AccountFacts) -> AccountFacts:
        return replace(
            facts,
            errors=tuple(self._redactor.redact(text) for text in facts.errors),
            notes=tuple(self._redactor.redact(text) for text in facts.notes),
        )

    # ── link monitor ─────────────────────────────────────────────────────────

    async def run_monitor(self) -> None:
        while True:
            await asyncio.gather(*(self.probe(name) for name in self._exchanges), return_exceptions=True)
            await asyncio.sleep(self._settings.probe_interval_s)

    async def probe(self, name: str, warm: bool = False) -> None:
        exchange = self._get(name)
        try:
            result = await asyncio.wait_for(exchange.adapter.probe_clock(), self._settings.request_timeout_s)
        except Exception as exc:
            exchange.failures += 1
            exchange.probe_error = self._redactor.redact(describe_error("ping", exc))
            if exchange.failures >= FAILURES_BEFORE_DOWN and exchange.link != "down":
                exchange.link = "down"
                self._journal.emit(Level.WARNING, "exchange", "link_down", exchange=name, error=exchange.probe_error)
            return

        if exchange.link == "unknown" and not warm:
            # The first round trip includes DNS and the TLS handshake, which skews both ping and clock offset.
            await self.probe(name, warm=True)
            return
        was = exchange.link
        exchange.failures = 0
        exchange.probe_error = None
        exchange.ping_ms = result.ping_ms
        exchange.clock_offset_ms = result.clock_offset_ms
        exchange.link = "up"
        self._submit_row(
            "latency_samples",
            {"ts": datetime.fromtimestamp(self._clock(), UTC), "exchange": name, "kind": "rest_ping", "ms": result.ping_ms},
        )
        if was != "up":
            self._journal.emit(
                Level.INFO,
                "exchange",
                "link_up",
                exchange=name,
                ping_ms=result.ping_ms,
                clock_offset_ms=result.clock_offset_ms,
            )

    # ── views ────────────────────────────────────────────────────────────────

    def _keys_state(self, exchange: _Exchange) -> str:
        if exchange.key_masked is None:
            return "none"
        if exchange.checking:
            return "checking"
        if exchange.check is None:
            return "saved"
        if not exchange.check["accepted"]:
            return "rejected"
        return "warning" if exchange.check["warnings"] else "ok"

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": exchange.name,
                "demo": exchange.demo,
                "link": exchange.link,
                "ping_ms": exchange.ping_ms,
                "clock_offset_ms": exchange.clock_offset_ms,
                "clock_warning": exchange.clock_offset_ms is not None
                and abs(exchange.clock_offset_ms) > self._settings.clock_warning_ms,
                "keys": self._keys_state(exchange),
                "read_only": self.read_only(exchange.name),
            }
            for exchange in self._exchanges.values()
        ]

    def describe_one(self, name: str) -> dict[str, Any]:
        exchange = self._get(name)
        return {
            "name": exchange.name,
            "demo": exchange.demo,
            "link": exchange.link,
            "ping_ms": exchange.ping_ms,
            "clock_offset_ms": exchange.clock_offset_ms,
            "probe_error": exchange.probe_error,
            "key_masked": exchange.key_masked,
            "keys": self._keys_state(exchange),
            "check": exchange.check,
            "read_only": self.read_only(exchange.name),
        }

    def describe(self) -> list[dict[str, Any]]:
        return [self.describe_one(name) for name in self._exchanges]

    async def close(self) -> None:
        for exchange in self._exchanges.values():
            try:
                await exchange.adapter.close()
            except Exception as exc:
                log.warning("closing %s client failed: %s", exchange.name, exc)
