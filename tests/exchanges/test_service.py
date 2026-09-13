import asyncio
from decimal import Decimal

import pytest

from app.config.settings import BinanceSettings, ExchangesSettings
from app.core.account import AccountFacts, KeyPermissions
from app.exchanges.base import ClockProbe
from app.exchanges.service import ExchangeService, InvalidKeys, UnknownExchange
from app.journal.journal import Journal
from app.keystore.keystore import Keystore
from app.system.log_setup import SecretRedactor
from tests.keystore.test_keystore import MemoryBackend

GOOD_FACTS = AccountFacts(
    permissions=KeyPermissions(reading=True, futures=True, withdrawals=False, ip_restricted=True),
    one_way_position_mode=True,
    wallet_usdt=Decimal("100"),
    available_usdt=Decimal("90"),
    taker_fee_pct=Decimal("0.05"),
    maker_fee_pct=Decimal("0.02"),
    ping_ms=300,
    clock_offset_ms=10,
)


class FakeAdapter:
    def __init__(self, name, key, secret):
        self.name = name
        self.key = key
        self.secret = secret
        self.closed = False
        self.probe_fails = False
        self.facts = GOOD_FACTS
        self.check_started = asyncio.Event()
        self.release_check: asyncio.Event | None = None

    async def close(self):
        self.closed = True

    async def probe_clock(self):
        if self.probe_fails:
            raise ConnectionError("unreachable")
        return ClockProbe(ping_ms=321, clock_offset_ms=15, server_ts_ms=0)

    async def check_account(self):
        self.check_started.set()
        if self.release_check is not None:
            await self.release_check.wait()
        return self.facts


class Harness:
    def __init__(self, settings: ExchangesSettings | None = None):
        self.backend = MemoryBackend()
        self.redactor = SecretRedactor()
        self.keystore = Keystore(self.redactor, self.backend)
        self.journal = Journal()
        self.events = []
        self.journal.add_sink(self.events.append)
        self.rows = []
        self.adapters: list[FakeAdapter] = []
        self.settings = settings or ExchangesSettings(probe_interval_s=10, request_timeout_s=1)

    def factory(self, name, key, secret, settings):
        adapter = FakeAdapter(name, key, secret)
        self.adapters.append(adapter)
        return adapter

    def build(self) -> ExchangeService:
        return ExchangeService(
            self.settings,
            self.keystore,
            self.journal,
            lambda table, row: self.rows.append((table, row)),
            self.redactor,
            adapter_factory=self.factory,
            clock=lambda: 1_789_300_000.0,
        )

    def types(self):
        return [(event.exchange, event.type) for event in self.events]


async def test_saved_keys_go_to_the_keystore_and_never_back_out():
    harness = Harness()
    service = harness.build()

    described = await service.save_keys("binance", "  binance-key-123456  ", "binance-secret-abcdef")

    assert harness.backend.values[("ludik", "binance:api_key")] == "binance-key-123456"
    assert harness.backend.values[("ludik", "binance:api_secret")] == "binance-secret-abcdef"
    assert described["key_masked"] == "bina…3456"
    assert "binance-secret-abcdef" not in str(described)
    assert "binance-secret-abcdef" not in str([event.payload for event in harness.events])
    assert harness.adapters[-1].key == "binance-key-123456"
    assert harness.adapters[0].closed


async def test_invalid_keys_are_refused_without_touching_the_keystore():
    harness = Harness()
    service = harness.build()
    with pytest.raises(InvalidKeys):
        await service.save_keys("mexc", "short", "with space inside")
    with pytest.raises(UnknownExchange):
        await service.save_keys("bybit", "long-enough-key", "long-enough-secret")
    assert harness.backend.values == {}


async def test_demo_mode_keeps_demo_keys_apart():
    harness = Harness(ExchangesSettings(binance=BinanceSettings(demo=True)))
    service = harness.build()

    await service.save_keys("binance", "demo-key-12345678", "demo-secret-12345678")

    assert ("ludik", "binance-demo:api_key") in harness.backend.values
    assert ("ludik", "binance:api_key") not in harness.backend.values
    assert service.snapshot()[0]["demo"] is True


async def test_keys_present_at_start_are_loaded_masked():
    harness = Harness()
    harness.backend.set_password("ludik", "mexc:api_key", "mexc-key-000011112222")
    harness.backend.set_password("ludik", "mexc:api_secret", "mexc-secret-000011112222")

    service = harness.build()

    mexc = service.describe_one("mexc")
    assert mexc["key_masked"] == "mexc…2222"
    assert mexc["keys"] == "saved"
    assert harness.redactor.redact("x mexc-secret-000011112222") == "x ***"


async def test_check_accepts_a_clean_account_and_journals_it():
    harness = Harness()
    service = harness.build()
    await service.save_keys("binance", "binance-key-123456", "binance-secret-abcdef")

    described = await service.check("binance")

    assert described["keys"] == "ok"
    assert described["check"]["accepted"] is True
    assert described["check"]["facts"]["wallet_usdt"] == "100"
    assert ("binance", "check_ok") in harness.types()


async def test_withdrawal_enabled_key_is_rejected():
    harness = Harness()
    service = harness.build()
    await service.save_keys("binance", "binance-key-123456", "binance-secret-abcdef")
    harness.adapters[-1].facts = AccountFacts(
        permissions=KeyPermissions(reading=True, futures=True, withdrawals=True, ip_restricted=True),
        one_way_position_mode=True,
        taker_fee_pct=Decimal("0.05"),
    )

    described = await service.check("binance")

    assert described["keys"] == "rejected"
    assert "withdrawals_enabled" in described["check"]["blocking"]
    assert ("binance", "check_rejected") in harness.types()


async def test_check_without_keys_is_refused():
    service = Harness().build()
    with pytest.raises(InvalidKeys):
        await service.check("mexc")


async def test_check_result_for_replaced_keys_is_discarded():
    harness = Harness()
    service = harness.build()
    await service.save_keys("mexc", "mexc-key-old-123456", "mexc-secret-old-123456")
    slow = harness.adapters[-1]
    slow.release_check = asyncio.Event()

    running = asyncio.create_task(service.check("mexc"))
    await slow.check_started.wait()
    await service.save_keys("mexc", "mexc-key-new-123456", "mexc-secret-new-123456")
    slow.release_check.set()
    await running

    assert service.describe_one("mexc")["check"] is None
    assert service.describe_one("mexc")["keys"] == "saved"


async def test_link_goes_down_only_after_two_failed_probes_and_recovers():
    harness = Harness()
    service = harness.build()
    binance = harness.adapters[0]

    await service.probe("binance")
    assert service.snapshot()[0]["link"] == "up"
    assert harness.rows[-1][0] == "latency_samples" and harness.rows[-1][1]["ms"] == 321

    binance.probe_fails = True
    await service.probe("binance")
    assert service.snapshot()[0]["link"] == "up"
    await service.probe("binance")
    assert service.snapshot()[0]["link"] == "down"

    binance.probe_fails = False
    await service.probe("binance")
    assert service.snapshot()[0]["link"] == "up"
    assert [kind for exchange, kind in harness.types() if exchange == "binance"] == ["link_up", "link_down", "link_up"]
