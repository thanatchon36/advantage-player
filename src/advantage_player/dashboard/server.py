"""FastAPI dashboard: password login (HMAC-signed session cookie), JSON API, WebSocket push,
CSV export. Controls only enqueue risk-reducing requests that the bot executes (rule 9)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import Engine
from starlette.websockets import WebSocketDisconnect

from advantage_player.dashboard import backend
from advantage_player.risk import control_queue

STATIC = Path(__file__).parent / "static"
COOKIE = "ap_session"
SESSION_S = 12 * 3600

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Advantage Player · login</title>
<style>body{font:15px system-ui;background:#0f1115;color:#e6e6e6;display:grid;
place-items:center;height:100vh;margin:0}form{display:flex;gap:8px}
input,button{font:inherit;padding:8px 12px;border-radius:6px;border:1px solid #333;
background:#1a1d24;color:inherit}p{color:#e66}</style></head><body>
<form method="post" action="/login"><input type="password" name="password"
placeholder="Dashboard password" autofocus><button>Log in</button></form>{err}</body></html>"""


class ControlIn(BaseModel):
    action: str
    args: dict[str, Any] = {}


def _sign(key: bytes, exp: int) -> str:
    return f"{exp}.{hmac.new(key, str(exp).encode(), hashlib.sha256).hexdigest()}"


def _valid(key: bytes, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s = token.split(".", 1)[0]
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(token, _sign(key, int(exp_s)))


def create_app(db: Engine, env: str, password: str, push_s: float = 2.0) -> FastAPI:
    if not password:
        raise RuntimeError("DASHBOARD_PASSWORD is empty — refusing to start the dashboard")
    key = hashlib.sha256(b"advantage-player-dashboard|" + password.encode()).digest()
    app = FastAPI(title="Advantage Player", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(request: Request) -> None:
        if not _valid(key, request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")

    @app.get("/", response_model=None)
    def index(request: Request) -> Response:
        if not _valid(key, request.cookies.get(COOKIE)):
            return HTMLResponse(LOGIN_HTML.replace("{err}", ""))
        return FileResponse(STATIC / "index.html")

    @app.post("/login", response_model=None)
    async def login(password_in: str = Form(alias="password")) -> Response:
        if not hmac.compare_digest(password_in.encode(), password.encode()):
            await asyncio.sleep(1.0)  # slow down guessing
            return HTMLResponse(LOGIN_HTML.replace("{err}", "<p>wrong password</p>"), 401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE,
            _sign(key, int(time.time()) + SESSION_S),
            max_age=SESSION_S,
            httponly=True,
            samesite="strict",
        )
        return resp

    @app.post("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    guarded = [Depends(auth)]

    @app.get("/api/snapshot", dependencies=guarded)
    def snapshot() -> dict[str, Any]:
        return backend.snapshot(db, env)

    @app.get("/api/tables", dependencies=guarded)
    def tables() -> dict[str, Any]:
        return backend.tables(db)

    @app.get("/api/ledger", dependencies=guarded)
    def ledger() -> list[dict[str, Any]]:
        return backend.ledger(db, env)

    @app.get("/api/game/{game_id}", dependencies=guarded)
    def game(game_id: str) -> dict[str, Any]:
        return backend.game(db, env, game_id)

    @app.get("/api/bankroll", dependencies=guarded)
    def bankroll(days: int = 30) -> dict[str, Any]:
        return backend.bankroll(db, env, days)

    @app.get("/api/chart/{symbol}", dependencies=guarded)
    def chart(symbol: str) -> dict[str, Any]:
        return backend.chart(db, env, symbol.upper())

    @app.get("/api/behavior", dependencies=guarded)
    def behavior(scope: str = "real", days: int = 30) -> dict[str, Any]:
        return backend.behavior(db, env, scope, days)

    @app.get("/api/discipline", dependencies=guarded)
    def discipline(hours: int = 24) -> dict[str, Any]:
        return backend.discipline(db, env, hours)

    @app.post("/api/control", dependencies=guarded)
    def control(body: ControlIn) -> dict[str, Any]:
        """JSON-only (no cross-site form posts) and SameSite=strict cookie."""
        try:
            rid = control_queue.enqueue(db, env, body.action, body.args)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"id": rid, "status": "pending"}

    @app.get("/api/export/{kind}.csv", dependencies=guarded)
    def export(kind: str, days: int = 30) -> Response:
        if kind not in backend.EXPORTS:
            raise HTTPException(404, f"unknown export {kind}")
        return Response(
            backend.export_csv(db, env, kind, days),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{env}_{kind}.csv"'},
        )

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        if not _valid(key, sock.cookies.get(COOKIE)):
            await sock.close(code=4401)
            return
        await sock.accept()
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                snap = await asyncio.to_thread(backend.snapshot, db, env)
                await sock.send_json(jsonable_encoder(snap))
                await asyncio.sleep(push_s)

    return app


async def serve(
    db: Engine,
    env: str,
    password: str,
    host: str,
    port: int,
    stop: asyncio.Event | None = None,
) -> None:
    """Standalone: uvicorn handles Ctrl-C. Embedded (`run --dashboard`): the bot owns the
    signal handlers and `stop` ends the server."""
    import uvicorn

    class Embedded(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    app = create_app(db, env, password)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = Embedded(config) if stop is not None else uvicorn.Server(config)

    async def watch() -> None:
        assert stop is not None
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch()) if stop is not None else None
    try:
        await server.serve()
    finally:
        if watcher is not None:
            watcher.cancel()
