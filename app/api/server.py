"""
VFP: The FastAPI application — session-token auth, loopback-only hosts and origins, REST reads, the live WebSocket and the built interface.
Changes when: a new endpoint appears or the rules for who may talk to the terminal change.
Anti-goal:
1. Any response carrying a raw secret — keys are only ever returned masked.
2. Accepting a request from a web page on another site — Host and Origin are checked, the token is required.
3. The token in URLs the server logs — the browser sends it in a header or the first WebSocket message.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import orjson
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from app.api.hub import Hub
from app.config.settings import ServerSettings
from app.exchanges.service import ExchangeService, InvalidKeys, UnknownExchange
from app.instruments.service import InstrumentService, UnknownPair
from app.core.schemas import LegSide
from app.execution.service import ExecutionService, TradingError
from app.portfolio.service import PortfolioError, PortfolioService
from app.storage.trades_history import TradeFilter
from app.strategies.price_gap.settings import FeedSettingsService, InvalidSetting

APP_NAME = "terminal-ludik"
AUTH_TIMEOUT_S = 3
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]

NOT_BUILT_PAGE = """<!doctype html><meta charset="utf-8"><title>Terminal Ludik</title>
<body style="background:#000;color:#fff;font:14px monospace;padding:2rem">
Интерфейс не собран. Выполните <code>npm install</code> и <code>npm run build</code> в папке <code>web</code>
или запустите терминал через <code>ludik.cmd</code>.</body>"""


class HistoryQueries(Protocol):
    async def summary(self, hours: int) -> dict[str, Any]: ...

    async def episodes(self, limit: int) -> list[dict[str, Any]]: ...

    async def storage(self) -> dict[str, Any]: ...

    async def trades(self, flt: TradeFilter, limit: int) -> list[dict[str, Any]]: ...

    async def trades_csv(self, flt: TradeFilter) -> str: ...

    async def trade_stats(self, flt: TradeFilter) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ApiContext:
    token: str
    version: str
    hub: Hub
    snapshot: Callable[[], dict[str, Any]]
    journal: Callable[[int], Awaitable[list[dict[str, Any]]]]
    exchanges: ExchangeService | None = None
    instruments: InstrumentService | None = None
    feed_settings: FeedSettingsService | None = None
    history: HistoryQueries | None = None
    portfolio: PortfolioService | None = None
    execution: ExecutionService | None = None


def allowed_origins(server: ServerSettings) -> frozenset[str]:
    ports = (server.port, server.dev_ui_port)
    return frozenset(f"http://{host}:{port}" for host in ALLOWED_HOSTS for port in ports)


def create_app(server: ServerSettings, web_dist: Path, context: ApiContext) -> FastAPI:
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    # Large uncompressed answers over loopback were cut off after ~19 s about one time in four on the dev machine
    # (antivirus web shield, 15.09.2026); compressed they pass. Websocket frames are not affected.
    app.add_middleware(GZipMiddleware, minimum_size=4096)
    origins = allowed_origins(server)

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value.encode(), context.token.encode()):
            raise HTTPException(status_code=401, detail="session token required")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        # Unauthenticated on purpose: a second launch uses it to find the running terminal. Nothing sensitive.
        return {"app": APP_NAME, "version": context.version}

    @app.get("/api/state", dependencies=[Depends(require_token)])
    async def state() -> dict[str, Any]:
        return context.snapshot()

    @app.get("/api/journal", dependencies=[Depends(require_token)])
    async def journal(limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
        return {"events": await context.journal(limit)}

    def exchanges() -> ExchangeService:
        if context.exchanges is None:
            raise HTTPException(status_code=503, detail="exchanges are not available")
        return context.exchanges

    @contextlib.contextmanager
    def exchange_errors():
        try:
            yield
        except UnknownExchange:
            raise HTTPException(status_code=404, detail="unknown exchange") from None
        except InvalidKeys as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/api/exchanges", dependencies=[Depends(require_token)])
    async def list_exchanges() -> dict[str, Any]:
        return {"exchanges": exchanges().describe()}

    @app.put("/api/exchanges/{name}/keys", dependencies=[Depends(require_token)])
    async def save_keys(name: str, request: Request) -> dict[str, Any]:
        # Parsed by hand: automatic validation errors would echo the submitted secret back in the response.
        try:
            body = orjson.loads(await request.body())
            api_key, api_secret = body["api_key"], body["api_secret"]
            if not isinstance(api_key, str) or not isinstance(api_secret, str):
                raise TypeError
        except (orjson.JSONDecodeError, KeyError, TypeError):
            raise HTTPException(status_code=400, detail="expected JSON with api_key and api_secret strings") from None
        with exchange_errors():
            return await exchanges().save_keys(name, api_key, api_secret)

    @app.delete("/api/exchanges/{name}/keys", dependencies=[Depends(require_token)])
    async def delete_keys(name: str) -> dict[str, Any]:
        with exchange_errors():
            return await exchanges().delete_keys(name)

    @app.post("/api/exchanges/{name}/check", dependencies=[Depends(require_token)])
    async def check_exchange(name: str) -> dict[str, Any]:
        with exchange_errors():
            return await exchanges().check(name)

    def instruments() -> InstrumentService:
        if context.instruments is None:
            raise HTTPException(status_code=503, detail="instruments are not available")
        return context.instruments

    @app.get("/api/pairs", dependencies=[Depends(require_token)])
    async def list_pairs() -> dict[str, Any]:
        return instruments().describe()

    @app.post("/api/pairs/flags", dependencies=[Depends(require_token)])
    async def pair_flags(request: Request) -> dict[str, Any]:
        try:
            body = orjson.loads(await request.body())
            key = body["key"]
            verified, blacklisted = body.get("manually_verified"), body.get("blacklisted")
            if not isinstance(key, str) or any(flag is not None and not isinstance(flag, bool) for flag in (verified, blacklisted)):
                raise TypeError
        except (orjson.JSONDecodeError, KeyError, TypeError):
            raise HTTPException(status_code=400, detail="expected key and optional boolean flags") from None
        try:
            return await instruments().set_flags(key, verified, blacklisted)
        except UnknownPair:
            raise HTTPException(status_code=404, detail="unknown pair") from None

    @app.post("/api/instruments/refresh", dependencies=[Depends(require_token)])
    async def refresh_instruments() -> dict[str, Any]:
        return await instruments().refresh()

    def feed_settings() -> FeedSettingsService:
        if context.feed_settings is None:
            raise HTTPException(status_code=503, detail="feed settings are not available")
        return context.feed_settings

    def history() -> HistoryQueries:
        if context.history is None:
            raise HTTPException(status_code=503, detail="history is not available")
        return context.history

    async def database_call(call: Awaitable[Any]) -> Any:
        try:
            return await call
        except ConnectionError:
            raise HTTPException(status_code=503, detail="database is not connected") from None

    @app.get("/api/history/summary", dependencies=[Depends(require_token)])
    async def history_summary(hours: int = Query(default=24, ge=1, le=24 * 90)) -> dict[str, Any]:
        return await database_call(history().summary(hours))

    @app.get("/api/history/episodes", dependencies=[Depends(require_token)])
    async def history_episodes(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        return {"episodes": await database_call(history().episodes(limit))}

    @app.get("/api/history/storage", dependencies=[Depends(require_token)])
    async def history_storage() -> dict[str, Any]:
        return await database_call(history().storage())

    def trade_filter(from_ms: int | None, to_ms: int | None, token: str | None, long: str | None, short: str | None) -> TradeFilter:
        now = datetime.now(UTC)
        since = datetime.fromtimestamp(from_ms / 1000, UTC) if from_ms else datetime(2000, 1, 1, tzinfo=UTC)
        until = datetime.fromtimestamp(to_ms / 1000, UTC) if to_ms else now + timedelta(days=1)
        return TradeFilter(since, until, (token or "").upper() or None, long or None, short or None)

    @app.get("/api/trades", dependencies=[Depends(require_token)])
    async def list_trades(
        from_ms: int | None = None,
        to_ms: int | None = None,
        token: str | None = Query(default=None, max_length=40),
        long: str | None = Query(default=None, max_length=20),
        short: str | None = Query(default=None, max_length=20),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, Any]:
        return {"trades": await database_call(history().trades(trade_filter(from_ms, to_ms, token, long, short), limit))}

    @app.get("/api/trades.csv", dependencies=[Depends(require_token)])
    async def export_trades(
        from_ms: int | None = None,
        to_ms: int | None = None,
        token: str | None = Query(default=None, max_length=40),
        long: str | None = Query(default=None, max_length=20),
        short: str | None = Query(default=None, max_length=20),
    ) -> PlainTextResponse:
        text = await database_call(history().trades_csv(trade_filter(from_ms, to_ms, token, long, short)))
        return PlainTextResponse(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="ludik-trades.csv"'})

    @app.get("/api/trades/stats", dependencies=[Depends(require_token)])
    async def trade_statistics(from_ms: int | None = None, to_ms: int | None = None) -> dict[str, Any]:
        return await database_call(history().trade_stats(trade_filter(from_ms, to_ms, None, None, None)))

    def portfolio() -> PortfolioService:
        if context.portfolio is None:
            raise HTTPException(status_code=503, detail="portfolio is not available")
        return context.portfolio

    @app.get("/api/portfolio", dependencies=[Depends(require_token)])
    async def get_portfolio() -> dict[str, Any]:
        return portfolio().snapshot()

    @app.post("/api/portfolio/pairs", dependencies=[Depends(require_token)])
    async def assign_pair(request: Request) -> dict[str, Any]:
        try:
            body = orjson.loads(await request.body())
            legs = [body["long"]["exchange"], body["long"]["symbol"], body["short"]["exchange"], body["short"]["symbol"]]
            if not all(isinstance(value, str) for value in legs):
                raise TypeError
        except (orjson.JSONDecodeError, KeyError, TypeError):
            raise HTTPException(status_code=400, detail="expected long and short legs with exchange and symbol") from None
        try:
            return await portfolio().assign_pair(*legs)
        except PortfolioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/portfolio/trades/{trade_id}/close-record", dependencies=[Depends(require_token)])
    async def close_record(trade_id: int) -> dict[str, Any]:
        try:
            return await portfolio().close_record(trade_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    def execution() -> ExecutionService:
        if context.execution is None:
            raise HTTPException(status_code=503, detail="trading is not available")
        return context.execution

    async def json_body(request: Request) -> dict[str, Any]:
        try:
            body = orjson.loads(await request.body())
        except orjson.JSONDecodeError:
            raise HTTPException(status_code=400, detail="expected a JSON object") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected a JSON object")
        return body

    async def trading_call(call: Awaitable[Any]) -> Any:
        try:
            return await call
        except TradingError as exc:
            raise HTTPException(status_code=409, detail={"reasons": exc.reasons}) from None

    @app.get("/api/trading/status", dependencies=[Depends(require_token)])
    async def trading_status() -> dict[str, Any]:
        return execution().status()

    @app.post("/api/trading/open", dependencies=[Depends(require_token)])
    async def trading_open(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        if not isinstance(body.get("pair_key"), str):
            raise HTTPException(status_code=400, detail="expected pair_key")
        return await trading_call(execution().open_pair(body["pair_key"]))

    @app.post("/api/trading/close", dependencies=[Depends(require_token)])
    async def trading_close(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        try:
            trade_id = int(body["trade_id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="expected trade_id") from None
        return await trading_call(execution().close_pair(trade_id))

    @app.post("/api/trading/close-leg", dependencies=[Depends(require_token)])
    async def trading_close_leg(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        try:
            trade_id, side = int(body["trade_id"]), LegSide(body["side"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="expected trade_id and side long|short") from None
        return await trading_call(execution().close_leg(trade_id, side))

    @app.post("/api/trading/close-all", dependencies=[Depends(require_token)])
    async def trading_close_all() -> dict[str, Any]:
        if not execution().status()["enabled"]:
            raise HTTPException(status_code=409, detail={"reasons": ["trading_disabled"]})
        return await execution().close_all()

    @app.get("/api/feed/settings", dependencies=[Depends(require_token)])
    async def get_feed_settings() -> dict[str, str]:
        return feed_settings().current()

    @app.put("/api/feed/settings", dependencies=[Depends(require_token)])
    async def put_feed_settings(request: Request) -> dict[str, str]:
        try:
            body = orjson.loads(await request.body())
            if not isinstance(body, dict):
                raise TypeError
            return await feed_settings().update(body)
        except (orjson.JSONDecodeError, TypeError):
            raise HTTPException(status_code=400, detail="expected a JSON object") from None
        except InvalidSetting as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        if websocket.headers.get("origin") not in origins:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        try:
            message = orjson.loads(await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_S))
            token = str(message.get("token", "")) if message.get("type") == "auth" else ""
        except (asyncio.TimeoutError, orjson.JSONDecodeError, AttributeError, RuntimeError):
            token = ""
        if not token or not secrets.compare_digest(token.encode(), context.token.encode()):
            await websocket.close(code=4401)
            return
        await context.hub.serve(websocket)

    if (web_dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def not_built() -> str:
            return NOT_BUILT_PAGE

    return app
