from pathlib import Path

from fastapi.testclient import TestClient

from app.api.hub import Hub
from app.api.server import ApiContext, create_app
from app.config.settings import ServerSettings, UiSettings
from app.execution.settings import TradingSettingsService
from app.journal.journal import Journal
from tests.scenarios.test_trading_scenarios import PAIR_KEY, harness

TOKEN = "test-session-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def client_for(tmp_path: Path, enabled: bool):
    h = await harness(enabled=enabled)

    async def no_events(limit):
        return []

    context = ApiContext(
        token=TOKEN, version="test", hub=Hub(lambda: {}, UiSettings()), snapshot=lambda: {}, journal=no_events,
        portfolio=h.portfolio, execution=h.execution,
    )
    return TestClient(create_app(ServerSettings(), tmp_path, context), base_url="http://127.0.0.1:8765"), h


async def test_trading_endpoints_need_the_token_and_explain_refusals(tmp_path):
    client, h = await client_for(tmp_path, enabled=False)

    assert client.post("/api/trading/open", json={"pair_key": PAIR_KEY}).status_code == 401
    refused = client.post("/api/trading/open", headers=AUTH, json={"pair_key": PAIR_KEY})
    assert refused.status_code == 409
    assert "trading_disabled" in refused.json()["detail"]["reasons"]
    assert client.post("/api/trading/close-all", headers=AUTH).status_code == 409
    assert client.post("/api/trading/close-leg", headers=AUTH, json={"trade_id": 1, "side": "sideways"}).status_code == 400
    assert h.mexc.sent == [] and h.binance.sent == []


class NoDatabase:
    ready = False


async def test_fast_trading_is_on_by_default_and_switches_without_a_restart(tmp_path):
    """The switch lives in the interface; the master switch [trading] enabled stays a file setting."""
    h = await harness(enabled=True)

    async def no_events(limit):
        return []

    service = TradingSettingsService(h.execution, NoDatabase(), Journal())
    context = ApiContext(
        token=TOKEN, version="test", hub=Hub(lambda: {}, UiSettings()), snapshot=lambda: {}, journal=no_events,
        portfolio=h.portfolio, execution=h.execution, trading_settings=service,
    )
    client = TestClient(create_app(ServerSettings(), tmp_path, context), base_url="http://127.0.0.1:8765")

    assert client.get("/api/trading/settings").status_code == 401
    assert client.get("/api/trading/settings", headers=AUTH).json() == {"fast_trading": "True"}

    assert client.put("/api/trading/settings", headers=AUTH, json={"fast_trading": False}).json() == {"fast_trading": "False"}
    assert h.execution.settings().fast_trading is False
    assert h.execution.status()["settings"]["fast_trading"] is False

    assert client.put("/api/trading/settings", headers=AUTH, json={"fast_trading": "nope"}).status_code == 400
    assert client.put("/api/trading/settings", headers=AUTH, json={"enabled": True}).status_code == 400
    assert h.execution.settings().enabled is True  # the master switch is untouched by the interface
