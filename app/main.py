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
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import uvicorn

from app.api.hub import Hub
from app.api.server import APP_NAME, ApiContext, create_app
from app.config.settings import REPO_ROOT, Settings, load_settings
from app.exchanges.service import ExchangeService
from app.instruments.service import InstrumentService
from app.market.binance_streams import BinanceStreams, aster_streams
from app.market.bingx_market import BingxMarket
from app.market.funding import POLL_S as FUNDING_POLL_S
from app.market.funding import FundingService
from app.market.gate_market import GateMarket
from app.market.mexc_market import MexcMarket
from app.market.variational_market import VariationalMarket
from app.market.state import MarketState
from app.market.radar_recorder import RadarRecorder
from app.execution.service import ExecutionService
from app.market.private_streams import OrderEvents, PrivateStreams
from app.portfolio.service import PortfolioService
from app.system.keep_awake import KeepAwake
from app.storage import history as history_queries
from app.storage import trades_history
from app.strategies.price_gap.engine import PriceGapEngine
from app.strategies.price_gap.recorder import EpisodeRecorder
from app.strategies.price_gap.settings import FeedSettingsService
from app.journal.journal import Journal, Level
from app.keystore.keystore import DB_PASSWORD, SESSION_TOKEN, Keystore
from app.storage.database import Database
from app.storage.events import recent_events
from app.storage.spool import Spool
from app.storage.writer import WriteQueue
from app.system.event_loop import loop_factory
from app.system.log_setup import SecretRedactor, configure_logging
from app.system.tls import use_system_trust_store

VERSION = "0.1.0"
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


async def _serve(
    settings: Settings, keystore: Keystore, redactor: SecretRedactor, token: str, open_browser: bool
) -> None:
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
    exchanges = ExchangeService(settings.exchanges, keystore, journal, writer.submit, redactor)
    instruments = InstrumentService(exchanges.names, exchanges.adapter, database, journal, settings.instruments)
    market = MarketState()
    feeds: dict[str, Any] = {}
    if "binance" in exchanges.names:
        binance_streams = BinanceStreams(market)
        feeds["binance"] = binance_streams
    if "mexc" in exchanges.names:
        feeds["mexc"] = MexcMarket(market, settings.feed.mexc_ticker_poll_ms)
    if "gate" in exchanges.names:
        feeds["gate"] = GateMarket(market, settings.feed.gate_ticker_poll_ms)
    if "aster" in exchanges.names:
        feeds["aster"] = aster_streams(market)
    if "bingx" in exchanges.names:
        feeds["bingx"] = BingxMarket(market, settings.feed.bingx_ticker_poll_ms, settings.feed.bingx_premium_poll_ms)
    if "variational" in exchanges.names:
        feeds["variational"] = VariationalMarket(
            market, lambda: exchanges.adapter("variational"), settings.exchanges.variational.poll_ms
        )

    funding = FundingService(
        exchanges.names,
        variational_stats=(lambda: exchanges.adapter("variational").stats(FUNDING_POLL_S)) if "variational" in exchanges.names else None,
    )

    def on_catalog(contracts: list[Any]) -> None:
        for name in ("binance", "aster"):
            if name in feeds:
                feeds[name].set_radar(item.symbol_raw for item in contracts if item.exchange == name)

    def taker_fee_pct(exchange: str) -> Decimal:
        account = exchanges.account_taker_fee_pct(exchange)
        return account if account is not None else settings.feed.default_taker_fee_pct(exchange)

    def feed_snapshot() -> dict[str, Any]:
        current = engine.settings()
        return {
            "size_usd": str(current.size_usd),
            "min_roi_pct": str(current.min_roi_pct),
            "taker_fee_pct": {exchange: str(taker_fee_pct(exchange)) for exchange in exchanges.names},
            "enter_after_ms": current.enter_after_ms,
            "exit_hysteresis_pct": str(current.exit_hysteresis_pct),
            "exit_after_ms": current.exit_after_ms,
        }

    keep_awake = KeepAwake()
    portfolio_settings = settings.portfolio
    if settings.trading.enabled:
        # Margin checks before an order rely on balances; keep them fresh while trading is on.
        portfolio_settings = portfolio_settings.model_copy(update={"balances_poll_s": min(portfolio_settings.balances_poll_s, 15)})

    def on_active_pairs(active: bool) -> None:
        if active:
            keep_awake.hold()
        else:
            keep_awake.release()

    portfolio = PortfolioService(
        exchanges,
        instruments,
        market,
        database,
        writer.submit,
        journal,
        portfolio_settings,
        taker_fee_pct,
        on_active_change=on_active_pairs,
        funding=funding.rate,
    )
    recorder = EpisodeRecorder(writer.submit, feed_snapshot)
    engine = PriceGapEngine(
        instruments,
        market,
        feeds,
        settings.feed,
        taker_fee_pct,
        on_catalog,
        sink=recorder,
        pinned=portfolio.pinned_pair_keys,
        fresh_ms_overrides={"variational": settings.exchanges.variational.max_quote_age_ms},
        funding=funding.rate,
    )
    feed_settings = FeedSettingsService(engine, database, journal)
    radar_recorder = RadarRecorder(instruments, market, writer.submit)
    order_events = OrderEvents()
    execution = ExecutionService(
        settings.trading,
        lambda: engine.settings().size_usd,
        exchanges,
        engine,
        portfolio,
        recorder,
        writer.submit,
        journal,
        taker_fee_pct,
        balance_max_age_ms=portfolio_settings.balances_poll_s * 2_000 + 5_000,
        order_events=order_events,
    )
    private_streams = PrivateStreams(
        exchanges.adapter,
        order_events,
        portfolio.poke,
        lambda: [item["name"] for item in exchanges.snapshot() if item["keys"] in ("ok", "warning")],
    )

    class History:
        async def summary(self, hours: int) -> dict[str, Any]:
            return await history_queries.summary(database.pool, hours)

        async def episodes(self, limit: int) -> list[dict[str, Any]]:
            return await history_queries.recent_episodes(database.pool, limit)

        async def storage(self) -> dict[str, Any]:
            return await history_queries.storage_usage(database.pool)

        async def trades(self, flt: Any, limit: int) -> list[dict[str, Any]]:
            return await trades_history.trades(database.pool, flt, limit)

        async def trades_csv(self, flt: Any) -> str:
            return await trades_history.trades_csv(database.pool, flt)

        async def trade_stats(self, flt: Any) -> dict[str, Any]:
            return await trades_history.trade_stats(database.pool, flt)

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
            "exchanges": exchanges.snapshot(),
            "instruments": instruments.summary(),
            "feed": {
                **engine.view(),
                "rows": execution.annotate(engine.view()["rows"]),
                "radar": execution.annotate(engine.view()["radar"]),
                "streams": {name: feed.stats() for name, feed in feeds.items()},
                "funding": funding.stats(),
            },
            "trading": {**execution.status(), "private_streams": private_streams.connected, "order_events": order_events.received},
            "history": {"recorded": recorder.recorded, "open": recorder.open_count, "radar_snapshots": radar_recorder.snapshots},
            "pairs": {"open": portfolio.open_count, "limit": settings.trading.max_open_pairs, "sleep_blocked": keep_awake.held},
            "portfolio": portfolio.snapshot(),
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
    context = ApiContext(
        token=token,
        version=VERSION,
        hub=hub,
        snapshot=snapshot,
        journal=journal_events,
        exchanges=exchanges,
        instruments=instruments,
        feed_settings=feed_settings,
        history=History(),
        portfolio=portfolio,
        execution=execution,
    )
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
    await feed_settings.load()
    if database.ready:
        closed = await history_queries.close_dangling_episodes(database.pool)
        if closed:
            journal.emit(Level.WARNING, "history", "dangling_episodes_closed", count=closed)
        await portfolio.load()
    background = [
        asyncio.create_task(writer.run()),
        asyncio.create_task(hub.run()),
        asyncio.create_task(exchanges.run_monitor()),
        asyncio.create_task(exchanges.check_all_with_keys()),
        asyncio.create_task(instruments.run()),
        *(asyncio.create_task(feed.run()) for feed in feeds.values()),
        asyncio.create_task(funding.run()),
        asyncio.create_task(engine.run()),
        asyncio.create_task(radar_recorder.run()),
        asyncio.create_task(portfolio.run_positions()),
        asyncio.create_task(portfolio.run_balances()),
        asyncio.create_task(portfolio.run_funding()),
        asyncio.create_task(portfolio.run_metrics()),
        asyncio.create_task(execution.run_warmup()),
        asyncio.create_task(private_streams.run()),
    ]
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
        if portfolio.open_count:
            journal.emit(Level.CRITICAL, "app", "stopped_with_open_pairs", pairs=portfolio.open_count)
        journal.emit(Level.INFO, "app", "stopped")
        server.should_exit = True
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(server_task), SHUTDOWN_TIMEOUT_S)
        for task in background:
            task.cancel()
        engine.close()
        await exchanges.close()
        await hub.close()
        await writer.close()
        await database.close()
        keep_awake.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ludik", description="Terminal Ludik")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--new-session", action="store_true", help="issue a new session token; open tabs must be reopened")
    args = parser.parse_args(argv)

    use_system_trust_store()
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

    # The token survives restarts so open tabs reconnect on their own; --new-session rotates it.
    token = None if args.new_session else keystore.get(SESSION_TOKEN)
    if not token:
        token = secrets.token_urlsafe(32)
        keystore.set(SESSION_TOKEN, token)
    try:
        asyncio.run(_serve(settings, keystore, redactor, token, open_browser), loop_factory=loop_factory())
    except KeyboardInterrupt:
        pass
    return 0
