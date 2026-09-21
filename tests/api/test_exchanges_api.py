from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.hub import Hub
from app.api.server import ApiContext, create_app
from app.config.settings import ExchangesSettings, ServerSettings, UiSettings
from tests.exchanges.test_service import Harness

TOKEN = "test-session-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "secret-value-that-must-not-echo"


@pytest.fixture
def setup(tmp_path: Path):
    harness = Harness(ExchangesSettings(request_timeout_s=1))
    service = harness.build()

    async def no_events(limit):
        return []

    context = ApiContext(
        token=TOKEN,
        version="test",
        hub=Hub(lambda: {}, UiSettings()),
        snapshot=lambda: {},
        journal=no_events,
        exchanges=service,
    )
    client = TestClient(create_app(ServerSettings(), tmp_path, context), base_url="http://127.0.0.1:8765")
    return client, harness


def test_key_endpoints_require_the_session_token(setup):
    client, harness = setup
    assert client.get("/api/exchanges").status_code == 401
    response = client.put("/api/exchanges/binance/keys", json={"api_key": "binance-key-123456", "api_secret": SECRET})
    assert response.status_code == 401
    assert harness.backend.values == {}


def test_save_returns_masked_key_and_never_the_secret(setup):
    client, harness = setup

    response = client.put(
        "/api/exchanges/binance/keys", headers=AUTH, json={"api_key": "binance-key-123456", "api_secret": SECRET}
    )

    assert response.status_code == 200
    assert response.json()["key_masked"] == "bina…3456"
    assert SECRET not in response.text
    listed = client.get("/api/exchanges", headers=AUTH)
    assert SECRET not in listed.text and "binance-key-123456" not in listed.text


def test_invalid_body_is_rejected_without_echoing_it(setup):
    client, _ = setup

    bad_type = client.put("/api/exchanges/binance/keys", headers=AUTH, json={"api_key": 123, "api_secret": SECRET})
    bad_format = client.put("/api/exchanges/binance/keys", headers=AUTH, json={"api_key": "short", "api_secret": SECRET + " x"})

    assert bad_type.status_code == 400 and SECRET not in bad_type.text
    assert bad_format.status_code == 400 and SECRET not in bad_format.text


def test_unknown_exchange_is_404(setup):
    client, _ = setup
    assert client.post("/api/exchanges/okx/check", headers=AUTH).status_code == 404


def test_check_and_delete_flow(setup):
    client, harness = setup
    client.put("/api/exchanges/mexc/keys", headers=AUTH, json={"api_key": "mexc-key-123456789", "api_secret": SECRET})

    checked = client.post("/api/exchanges/mexc/check", headers=AUTH).json()
    assert checked["keys"] == "ok"

    deleted = client.delete("/api/exchanges/mexc/keys", headers=AUTH).json()
    assert deleted["keys"] == "none"
    assert harness.backend.values == {}
