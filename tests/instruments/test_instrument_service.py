from decimal import Decimal
from types import SimpleNamespace

from app.config.settings import InstrumentsSettings
from app.core.pairs import Quote
from app.instruments.service import InstrumentService
from app.journal.journal import Journal
from tests.core.helpers import instrument


class Adapter:
    def __init__(self, exchange, symbol):
        self.fail = False
        self.item = instrument(exchange, symbol, "SOL", min_notional_usd="0")

    async def load_instruments(self):
        if self.fail:
            raise ConnectionError("timeout")
        return [self.item]

    async def fetch_quotes(self):
        return {self.item.symbol_raw: Quote(bid=Decimal("99.9"), ask=Decimal("100.1"), index=Decimal("100"), volume24h_usd=Decimal("1000000"))}


async def test_three_exchanges_make_three_pairs_and_a_failing_one_keeps_its_last_load():
    adapters = {"binance": Adapter("binance", "SOLUSDT"), "mexc": Adapter("mexc", "SOL_USDT"), "gate": Adapter("gate", "SOL_USDT")}
    events = []
    journal = Journal()
    journal.add_sink(events.append)
    service = InstrumentService(["binance", "mexc", "gate"], adapters.__getitem__, SimpleNamespace(ready=False), journal, InstrumentsSettings())

    await service.refresh()
    keys = sorted(record.key for record in service.records())
    assert keys == ["binance:SOLUSDT|gate:SOL_USDT", "binance:SOLUSDT|mexc:SOL_USDT", "mexc:SOL_USDT|gate:SOL_USDT"]

    adapters["gate"].fail = True
    summary = await service.refresh()
    assert summary["pairs"] == 3
    assert "gate" in summary["error"]
    assert [event.type for event in events] == ["refreshed", "refresh_failed", "refreshed"]
    assert events[0].payload["contracts"] == {"binance": 1, "mexc": 1, "gate": 1}
