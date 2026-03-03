# be-conductor — Local orchestration for terminal sessions.
#
# Copyright (c) 2026 Max Rheiner / Somniacs AG
#
# Licensed under the MIT License. You may obtain a copy
# of the license at:
#
#     https://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

"""FastAPI application — CORS, API router, and static dashboard."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from be_conductor.api.routes import router, registry
from be_conductor.utils.config import CONDUCTOR_TOKEN, HOST, PORT, PID_FILE, VERSION, ensure_dirs


# ---------------------------------------------------------------------------
# Bearer token auth middleware (only active when CONDUCTOR_TOKEN is set)
# ---------------------------------------------------------------------------

# Paths that never require auth
_PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/"}
_PUBLIC_PREFIXES = ("/static/",)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for public paths
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        # Skip WebSocket upgrades — auth is handled in the WS handler via
        # _check_ws_auth() which supports query-param tokens (browsers can't
        # set Authorization headers on WebSocket connections).
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Check Bearer token
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {CONDUCTOR_TOKEN}":
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    PID_FILE.write_text(str(os.getpid()))

    # Reconcile worktree state (crash recovery)
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, registry.worktree_manager.reconcile
        )
        if any(result.values()):
            import logging
            log = logging.getLogger("be_conductor.worktrees")
            log.info("Worktree reconcile: %s", result)
    except Exception:
        pass

    yield
    await registry.cleanup_all()
    PID_FILE.unlink(missing_ok=True)


def create_app() -> FastAPI:
    app = FastAPI(title="Be-Conductor", version=VERSION, lifespan=lifespan)

    # CORS: Allow any Be-Conductor dashboard to connect cross-origin.
    # Safe on private Tailscale networks where the network is the trust boundary.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth middleware — only when CONDUCTOR_TOKEN is set
    if CONDUCTOR_TOKEN:
        app.add_middleware(BearerAuthMiddleware)

    app.include_router(router)

    # Serve dashboard
    static_dir = Path(__file__).parent.parent.parent / "static"
    if static_dir.exists():

        @app.get("/")
        async def dashboard():
            if CONDUCTOR_TOKEN:
                # Inject auth token meta tag so the dashboard can authenticate
                html = (static_dir / "index.html").read_text()
                html = html.replace(
                    "<head>",
                    f'<head>\n    <meta name="be-conductor-token" content="{CONDUCTOR_TOKEN}">',
                    1,
                )
                return HTMLResponse(html)
            return FileResponse(static_dir / "index.html")

        @app.get("/sw.js")
        async def service_worker():
            return FileResponse(
                static_dir / "sw.js",
                media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/"},
            )

        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir)),
            name="static",
        )

    return app


app = create_app()


def run_server(host: str = HOST, port: int = PORT):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
