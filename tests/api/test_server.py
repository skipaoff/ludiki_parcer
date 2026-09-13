from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.hub import Hub
from app.api.server import ApiContext, create_app
from app.config.settings import ServerSettings, UiSettings
from app.journal.journal import Journal, Level

TOKEN = "test-session-token-0123456789"
ORIGIN = "http://127.0.0.1:8765"
WS_URL = "ws://127.0.0.1:8765/ws"


@pytest.fixture
def journal():
    return Journal()


@pytest.fixture
def client(tmp_path: Path, journal: Journal):
    state = {"database": {"status": "ok"}, "pairs": {"open": 0, "limit": 3}}
    hub = Hub(lambda: state, UiSettings())
    journal.add_sink(hub.push_event)

    async def events(limit):
        return [event.to_wire() for event in journal.recent(limit)]

    context = ApiContext(token=TOKEN, version="test", hub=hub, snapshot=lambda: state, journal=events)
    app = create_app(ServerSettings(), tmp_path / "missing-dist", context)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_open_and_says_nothing_sensitive(client):
    assert client.get("/api/health").json() == {"app": "terminal-ludik", "version": "test"}


def test_state_and_journal_require_the_session_token(client, journal):
    journal.emit(Level.INFO, "app", "started")

    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers=auth("wrong")).status_code == 401
    assert client.get("/api/state", headers=auth()).json()["pairs"] == {"open": 0, "limit": 3}
    assert [event["type"] for event in client.get("/api/journal", headers=auth()).json()["events"]] == ["started"]


def test_foreign_host_header_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_unbuilt_interface_explains_how_to_build_it(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "npm run build" in response.text


def test_websocket_sends_hello_then_live_events(client, journal):
    with client.websocket_connect(WS_URL, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"type": "auth", "token": TOKEN})
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["state"]["database"]["status"] == "ok"

        journal.emit(Level.CRITICAL, "execution", "leg_lost", exchange="mexc")
        message = socket.receive_json()
        assert message["type"] == "event"
        assert message["event"]["type"] == "leg_lost"
        assert message["event"]["level"] == "critical"


def test_websocket_with_wrong_token_is_closed(client):
    with client.websocket_connect(WS_URL, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4401


def test_websocket_from_foreign_origin_is_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS_URL, headers={"Origin": "http://evil.example"}) as socket:
            socket.receive_json()
