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
import be_conductor.utils.config as _config
from be_conductor.utils.config import HOST, PORT, PID_FILE, VERSION, ensure_dirs


# ---------------------------------------------------------------------------
# Bearer token auth middleware — always mounted, no-op when no token is set.
# Reads CONDUCTOR_TOKEN from the config module dynamically so token changes
# (e.g. via the admin API) take effect immediately without a restart.
# ---------------------------------------------------------------------------

# Paths that never require auth
_PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/"}
_PUBLIC_PREFIXES = ("/static/",)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = _config.CONDUCTOR_TOKEN
        if not token:
            return await call_next(request)  # no token configured → open access

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
        if auth == f"Bearer {token}":
            return await call_next(request)

        # Query-param token fallback for paths that can't set headers
        # (e.g. PDF iframes on /files/read)
        if path == "/files/read":
            qp_token = request.query_params.get("token")
            if qp_token == token:
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
def _cleanup_orphaned_notes():
    """Remove session-scoped notes whose session no longer exists."""
    from be_conductor.notes import store as notes_store
    valid = set(registry.sessions.keys()) | set(registry.resumable.keys())
    count = notes_store.cleanup_orphaned(valid)
    if count:
        import logging
        logging.getLogger("be_conductor.notes").info(
            "Cleaned up %d orphaned session note(s)", count
        )


async def _notes_cleanup_loop():
    """Periodically clean up orphaned notes (every 10 min)."""
    import asyncio
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(600)
        try:
            await loop.run_in_executor(None, _cleanup_orphaned_notes)
        except Exception:
            pass


async def lifespan(app: FastAPI):
    import asyncio

    ensure_dirs()
    PID_FILE.write_text(str(os.getpid()))

    loop = asyncio.get_event_loop()

    # Reconcile worktree state (crash recovery)
    try:
        result = await loop.run_in_executor(
            None, registry.worktree_manager.reconcile
        )
        if any(result.values()):
            import logging
            log = logging.getLogger("be_conductor.worktrees")
            log.info("Worktree reconcile: %s", result)
    except Exception:
        pass

    # Clean up orphaned notes on startup
    try:
        await loop.run_in_executor(None, _cleanup_orphaned_notes)
    except Exception:
        pass

    # Start periodic notes cleanup
    cleanup_task = asyncio.create_task(_notes_cleanup_loop())

    yield

    cleanup_task.cancel()
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

    # Auth middleware — always mounted; becomes active when a token is set
    app.add_middleware(BearerAuthMiddleware)

    app.include_router(router)

    # Serve dashboard
    static_dir = Path(__file__).parent.parent.parent / "static"
    if static_dir.exists():

        @app.get("/")
        async def dashboard():
            token = _config.CONDUCTOR_TOKEN
            if token:
                # Inject auth token meta tag so the dashboard can authenticate
                html = (static_dir / "index.html").read_text()
                html = html.replace(
                    "<head>",
                    f'<head>\n    <meta name="be-conductor-token" content="{token}">',
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


def run_server(host: str = HOST, port: int = PORT,
               ssl_certfile: str | None = None, ssl_keyfile: str | None = None):
    import uvicorn
    import be_conductor.utils.config as _cfg

    certfile = ssl_certfile or _cfg.SSL_CERTFILE
    keyfile = ssl_keyfile or _cfg.SSL_KEYFILE
    kwargs: dict = dict(host=host, port=port, log_level="info")
    if certfile and keyfile:
        kwargs["ssl_certfile"] = certfile
        kwargs["ssl_keyfile"] = keyfile
    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    run_server()
