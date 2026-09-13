"""
VFP: Composition root — builds every component from settings, runs them on one event loop and shuts them down in order.
Changes when: a component is added or removed, or the startup and shutdown order changes.
Anti-goal:
1. Behaviour beyond wiring and lifecycle — each component owns its logic.
2. Two terminals at once — a second launch opens the running terminal in the browser and exits.
3. The session token on screen or in logs — it travels to the browser only inside the opened URL fragment.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import socket
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import orjson
import uvicorn

from app.api.hub import Hub
from app.api.server import APP_NAME, ApiContext, create_app
from app.config.settings import REPO_ROOT, Settings, load_settings
from app.journal.journal import Journal, Level
from app.keystore.keystore import DB_PASSWORD, SESSION_TOKEN, Keystore, api_key_name
from app.storage.database import Database
from app.storage.events import recent_events
from app.storage.spool import Spool
from app.storage.writer import WriteQueue
from app.system.event_loop import loop_factory
from app.system.log_setup import SecretRedactor, configure_logging

VERSION = "0.0.1"
EXCHANGES = ("binance", "mexc")
SHUTDOWN_TIMEOUT_S = 10

log = logging.getLogger("app")


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # Ctrl+C belongs to asyncio.run, which cancels the main task so the terminal shuts down in order.
        yield


def _base_url(settings: Settings) -> str:
    host = "[::1]" if settings.server.host == "::1" else settings.server.host
    return f"http://{host}:{settings.server.port}"


def _probe_running(settings: Settings) -> bool | None:
    """None when the port is free, True when our terminal answers on it, False when something else holds it."""
    try:
        with socket.create_connection((settings.server.host, settings.server.port), timeout=0.5):
            pass
    except OSError:
        return None
    try:
        with urllib.request.urlopen(f"{_base_url(settings)}/api/health", timeout=2) as response:
            return orjson.loads(response.read()).get("app") == APP_NAME
    except Exception:
        return False


def _level_for(kind: str) -> Level:
    return Level.WARNING if kind == "db_unavailable" else Level.INFO


async def _serve(settings: Settings, keystore: Keystore, token: str, open_browser: bool) -> None:
    started_ms = int(time.time() * 1000)
    journal = Journal(settings.ui.journal_buffer)
    database = Database(settings.database, lambda: keystore.get(DB_PASSWORD), REPO_ROOT / "db", settings.paths.logs_dir)
    writer = WriteQueue(
        database,
        Spool(settings.paths.spool_dir),
        settings.storage,
        notify=lambda kind, detail: journal.emit(_level_for(kind), "storage", kind, **detail),
    )
    journal.add_sink(lambda event: writer.submit("events", event.to_row()))
    keys_present = {exchange: keystore.get(api_key_name(exchange)) is not None for exchange in EXCHANGES}

    def snapshot() -> dict[str, Any]:
        return {
            "app": {"version": VERSION, "started_ts_ms": started_ms},
            "database": {
                "status": "ok" if database.ready else "down",
                "error": None if database.ready else database.last_error,
                "pending_rows": writer.pending_rows,
                "spooled_rows": writer.spooled_rows,
                "rejected_rows": writer.rejected_rows,
            },
            "exchanges": [
                {"name": exchange, "status": "configured" if keys_present[exchange] else "not_configured"}
                for exchange in EXCHANGES
            ],
            "pairs": {"open": 0, "limit": 3},
        }

    async def journal_events(limit: int) -> list[dict[str, Any]]:
        memory = [event.to_wire() for event in journal.recent(limit)]
        if not database.ready:
            return memory
        try:
            stored = await recent_events(database.pool, limit)
        except Exception as exc:
            log.warning("journal read failed: %s", exc)
            return memory
        newest = stored[-1]["ts_ms"] if stored else -1
        return (stored + [event for event in memory if event["ts_ms"] > newest])[-limit:]

    hub = Hub(snapshot, settings.ui)
    journal.add_sink(hub.push_event)
    context = ApiContext(token=token, version=VERSION, hub=hub, snapshot=snapshot, journal=journal_events)
    app = create_app(settings.server, settings.paths.web_dist, context)
    server = _Server(
        uvicorn.Config(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_config=None,
            access_log=False,
            lifespan="off",
            server_header=False,
        )
    )

    journal.emit(Level.INFO, "app", "started", version=VERSION, pid=os.getpid())
    await writer.flush(force_connect=True)
    background = [asyncio.create_task(writer.run()), asyncio.create_task(hub.run())]
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started and not server_task.done():
            await asyncio.sleep(0.05)
        if server_task.done():
            server_task.result()
            raise RuntimeError("web server stopped during startup")
        url = _base_url(settings)
        print(f"Terminal Ludik {VERSION}: {url}  (Ctrl+C to stop)", flush=True)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, f"{url}/#t={token}")
        # asyncio.wait, not await: cancelling the main task must not cancel the server mid-request.
        await asyncio.wait({server_task})
    finally:
        journal.emit(Level.INFO, "app", "stopped")
        server.should_exit = True
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(server_task), SHUTDOWN_TIMEOUT_S)
        for task in background:
            task.cancel()
        await hub.close()
        await writer.close()
        await database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ludik", description="Terminal Ludik")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    redactor = SecretRedactor()
    configure_logging(settings.paths.logs_dir, settings.logging.level, redactor)
    keystore = Keystore(redactor)
    open_browser = settings.server.open_browser and not args.no_browser

    running = _probe_running(settings)
    if running is True:
        token = keystore.get(SESSION_TOKEN)
        print(f"Terminal Ludik is already running: {_base_url(settings)}", flush=True)
        if open_browser and token:
            webbrowser.open(f"{_base_url(settings)}/#t={token}")
        return 0
    if running is False:
        log.error("port %s is taken by another program; change [server] port in config.toml", settings.server.port)
        return 2

    token = secrets.token_urlsafe(32)
    keystore.set(SESSION_TOKEN, token)
    try:
        asyncio.run(_serve(settings, keystore, token, open_browser), loop_factory=loop_factory())
    except KeyboardInterrupt:
        pass
    return 0
