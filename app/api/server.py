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
from pathlib import Path
from typing import Any, Awaitable, Callable

import orjson
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from app.api.hub import Hub
from app.config.settings import ServerSettings
from app.exchanges.service import ExchangeService, InvalidKeys, UnknownExchange

APP_NAME = "terminal-ludik"
AUTH_TIMEOUT_S = 3
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]

NOT_BUILT_PAGE = """<!doctype html><meta charset="utf-8"><title>Terminal Ludik</title>
<body style="background:#000;color:#fff;font:14px monospace;padding:2rem">
Интерфейс не собран. Выполните <code>npm install</code> и <code>npm run build</code> в папке <code>web</code>
или запустите терминал через <code>ludik.cmd</code>.</body>"""


@dataclass(frozen=True, slots=True)
class ApiContext:
    token: str
    version: str
    hub: Hub
    snapshot: Callable[[], dict[str, Any]]
    journal: Callable[[int], Awaitable[list[dict[str, Any]]]]
    exchanges: ExchangeService | None = None


def allowed_origins(server: ServerSettings) -> frozenset[str]:
    ports = (server.port, server.dev_ui_port)
    return frozenset(f"http://{host}:{port}" for host in ALLOWED_HOSTS for port in ports)


def create_app(server: ServerSettings, web_dist: Path, context: ApiContext) -> FastAPI:
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
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
