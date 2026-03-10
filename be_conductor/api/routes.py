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

"""REST and WebSocket API routes for session management and server info."""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

# Session names: alphanumeric, hyphens, underscores, spaces, dots. Max 64 chars.
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _.~-]{0,63}$")

from be_conductor.external.observer import SessionObserver
from be_conductor.external.scanner import ExternalSessionScanner
from be_conductor.notes import store as notes_store
from be_conductor.notes import export as notes_export
from be_conductor.notifications.manager import NotificationEvent
from urllib.parse import quote

from be_conductor.notifications.webhook import send_webhook, test_webhook
from be_conductor.sessions.registry import SessionRegistry
from be_conductor.utils import config as cfg
from be_conductor.utils.config import PORT, UPLOADS_DIR, VERSION

router = APIRouter()
registry = SessionRegistry()
_external_scanner = ExternalSessionScanner()

# Active observers for external session observation (file_id → SessionObserver)
_observers: dict[str, SessionObserver] = {}

# Set of active WebSocket connections for notification broadcast.
_notification_ws: dict[WebSocket, str] = {}  # ws → session_id

# Notification ack — set by browser when it sees a notification (tab visible).
_notification_ack: asyncio.Event = asyncio.Event()

# Cached dashboard base URL (avoids Tailscale subprocess on every notification).
_dashboard_base_url: str | None = None


def _get_dashboard_base_url() -> str:
    """Return the base URL for dashboard deep links (cached after first call)."""
    global _dashboard_base_url
    if _dashboard_base_url is not None:
        return _dashboard_base_url
    host = _get_tailscale_name() or _get_tailscale_ip() or "127.0.0.1"
    scheme = "https" if cfg.SSL_CERTFILE else "http"
    _dashboard_base_url = f"{scheme}://{host}:{PORT}"
    return _dashboard_base_url


async def _broadcast_notification(event: NotificationEvent):
    """Send a notification event to all connected WebSocket clients.

    Also dispatches to configured webhooks.
    """
    # WebSocket broadcast — send as a specially-prefixed text message
    msg = json.dumps({
        "type": "notification",
        "session_id": event.session_id,
        "session_name": event.session_name,
        "reason": event.reason,
        "snippet": event.snippet,
        "timestamp": event.timestamp,
    })
    dead: list[WebSocket] = []
    delivered = 0
    for ws in list(_notification_ws):
        try:
            await ws.send_text(msg)
            delivered += 1
        except Exception:
            dead.append(ws)
    for ws in dead:
        _notification_ws.pop(ws, None)

    # If notification was delivered to at least one browser, wait briefly for
    # a visibility ack.  If the user is actually looking at the dashboard the
    # ack arrives in milliseconds and we skip the webhook (like WhatsApp read
    # receipts).  If the tab is hidden / minimised no ack comes and we fall
    # through to the webhook after the timeout.
    if delivered > 0:
        _notification_ack.clear()
        try:
            await asyncio.wait_for(_notification_ack.wait(), timeout=2.0)
            return  # user saw it — skip webhook
        except asyncio.TimeoutError:
            pass  # tab not visible — proceed to webhook

    # Webhook dispatch — single global webhook config
    wh = registry.notification_manager.get_webhook_settings()
    url = wh.get("webhook_url", "")
    enabled = wh.get("webhook_enabled", False)
    if url and enabled:
        base = _get_dashboard_base_url()
        dashboard_url = f"{base}#session={quote(event.session_name)}"
        asyncio.ensure_future(send_webhook(
            url=url,
            session_name=event.session_name,
            reason=event.reason,
            snippet=event.snippet,
            chat_id=wh.get("webhook_chat_id"),
            dashboard_url=dashboard_url,
        ))


# Register the broadcast handler with the notification manager.
registry.notification_manager.register_handler(_broadcast_notification)


async def _broadcast_resize(session_id: str, rows: int, cols: int,
                            source: str | None, owner_id: str | None = None):
    """Send a resize event to all browser WebSockets watching this session."""
    msg = json.dumps({
        "type": "resize",
        "rows": rows,
        "cols": cols,
        "source": source,
        "owner": owner_id,
    })
    dead: list[WebSocket] = []
    for ws, sid in list(_notification_ws.items()):
        if sid != session_id:
            continue
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _notification_ws.pop(ws, None)


def _allowed_base_commands() -> set[str]:
    """Build set of allowed base commands from current config (re-evaluated on each call)."""
    return {shlex.split(c["command"])[0] for c in cfg.ALLOWED_COMMANDS}

# Content-type to file extension fallback mapping
_MIME_EXTENSIONS: dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/bmp": "bmp", "image/svg+xml": "svg",
    "application/pdf": "pdf", "text/plain": "txt", "text/csv": "csv",
    "text/html": "html", "text/markdown": "md",
    "application/json": "json", "application/xml": "xml",
    "application/zip": "zip", "application/gzip": "gz",
}


# ---------------------------------------------------------------------------
# Key mapping — human-readable names to terminal escape sequences
# ---------------------------------------------------------------------------

_KEY_MAP: dict[str, str] = {
    "ENTER": "\r",
    "TAB": "\t",
    "ESCAPE": "\x1b",
    "BACKSPACE": "\x7f",
    "UP": "\x1b[A",
    "DOWN": "\x1b[B",
    "RIGHT": "\x1b[C",
    "LEFT": "\x1b[D",
    "CTRL+C": "\x03",
    "CTRL+D": "\x04",
    "CTRL+Z": "\x1a",
    "CTRL+L": "\x0c",
    "CTRL+A": "\x01",
    "CTRL+E": "\x05",
    "CTRL+K": "\x0b",
    "CTRL+U": "\x15",
    "CTRL+W": "\x17",
    "CTRL+R": "\x12",
    "CTRL+\\": "\x1c",
}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    name: str
    command: str
    cwd: str | None = None
    source: str | None = None  # "cli" bypasses whitelist; dashboard is restricted
    env: dict[str, str] | None = None
    rows: int | None = None  # initial PTY size (avoids resize race on startup)
    cols: int | None = None
    worktree: bool = False  # create an isolated git worktree for this session


class InputRequest(BaseModel):
    text: str | None = None
    keys: list[str] | None = None


class ResumeRequest(BaseModel):
    rows: int | None = None
    cols: int | None = None


class StopRequest(BaseModel):
    mode: str = "kill"  # "kill" = hard stop, "graceful" = SIGINT (allows resume), "forget" = SIGINT + discard


class ResizeRequest(BaseModel):
    rows: int
    cols: int
    source: str | None = None
    client_id: str | None = None


# ---------------------------------------------------------------------------
# Response models (gives typed OpenAPI schemas)
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    ok: bool
    version: str


class SessionResponse(BaseModel):
    id: str
    name: str
    command: str
    status: str
    pid: int | None = None
    start_time: float | None = None
    created_at: str | None = None
    exit_code: int | None = None
    cwd: str | None = None
    rows: int | None = None
    cols: int | None = None
    resize_source: str | None = None
    resize_owner: str | None = None
    cli_attach_count: int | None = None
    resume_id: str | None = None
    resume_flag: str | None = None
    resume_command: str | None = None
    ws_url: str | None = None
    worktree: dict | None = None


class StatusResponse(BaseModel):
    status: str


class UploadResponse(BaseModel):
    path: str
    filename: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_tailscale_ip() -> str | None:
    """Get the machine's Tailscale IPv4 address, if available."""
    if not shutil.which("tailscale"):
        return None
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return None


def _get_tailscale_name() -> str | None:
    """Get the machine's Tailscale MagicDNS name, if available."""
    if not shutil.which("tailscale"):
        return None
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            status = json.loads(result.stdout)
            dns_name = status.get("Self", {}).get("DNSName", "")
            if dns_name:
                return dns_name.rstrip(".")
    except Exception:
        pass
    return None


def _ws_url_for(request: Request, session_id: str) -> str:
    """Build the WebSocket URL for a session based on the incoming request."""
    host = request.headers.get("host", f"127.0.0.1:{PORT}")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    return f"{scheme}://{host}/sessions/{session_id}/stream"


def _check_ws_auth(ws: WebSocket) -> bool:
    """Validate WebSocket auth when CONDUCTOR_TOKEN is set. Returns True if ok."""
    expected = cfg.CONDUCTOR_TOKEN
    if not expected:
        return True
    # Check Authorization header
    auth = ws.headers.get("authorization", "")
    if auth == f"Bearer {expected}":
        return True
    # Check query parameter
    token = ws.query_params.get("token", "")
    if token == expected:
        return True
    return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health():
    """Health/discovery endpoint — always public, no auth required."""
    return {"ok": True, "version": VERSION}


@router.get("/info")
async def server_info(request: Request):
    """Return server identity for multi-server dashboard."""
    loop = asyncio.get_event_loop()
    ts_ip, ts_name = await asyncio.gather(
        loop.run_in_executor(None, _get_tailscale_ip),
        loop.run_in_executor(None, _get_tailscale_name),
    )
    # Tell the frontend whether this client has admin access
    host = request.client.host if request.client else ""
    is_local = host in ("127.0.0.1", "::1")
    is_admin = is_local or bool(cfg.CONDUCTOR_TOKEN)
    return {
        "hostname": socket.gethostname(),
        "port": PORT,
        "version": VERSION,
        "tailscale_ip": ts_ip,
        "tailscale_name": ts_name,
        "is_admin": is_admin,
    }


@router.get("/tailscale/peers")
async def tailscale_peers():
    """Return online Tailscale peers for the server picker."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_tailscale_peers)


def _get_tailscale_peers():
    """Blocking helper — runs in thread pool."""
    if not shutil.which("tailscale"):
        return []
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        status = json.loads(result.stdout)
        peers = []
        for peer in (status.get("Peer") or {}).values():
            ips = peer.get("TailscaleIPs", [])
            ipv4 = next((ip for ip in ips if "." in ip), None)
            if not ipv4:
                continue
            dns_name = (peer.get("DNSName") or "").rstrip(".")
            hostname = peer.get("HostName", "")
            # Some devices (e.g. Android) report "localhost" — derive a
            # meaningful name from the MagicDNS name instead.
            if (not hostname or hostname == "localhost") and dns_name:
                hostname = dns_name.split(".")[0]
            peers.append({
                "hostname": hostname,
                "dns_name": dns_name,
                "ip": ipv4,
                "online": bool(peer.get("Online")),
            })
        return sorted(peers, key=lambda p: (not p["online"], p["hostname"].lower()))
    except Exception:
        return []


@router.get("/config")
async def get_config():
    """Return allowed commands and directories for the dashboard."""
    # Only expose frontend-safe fields (not stop_sequence with raw escapes).
    safe = [
        {k: v for k, v in c.items() if k != "stop_sequence"}
        for c in cfg.ALLOWED_COMMANDS
    ]
    return {
        "allowed_commands": safe,
        "default_directories": cfg.DEFAULT_DIRECTORIES,
        "upload_warn_size": cfg.UPLOAD_WARN_SIZE,
        "config_version": cfg.get_config_version(),
    }


@router.get("/browse")
async def browse_directory(path: str = "~"):
    """List subdirectories of a given path for the directory picker."""
    try:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="Not a directory")

        dirs = []
        try:
            for entry in sorted(resolved.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
        except PermissionError:
            pass

        return {
            "current": str(resolved),
            "parent": str(resolved.parent) if resolved != resolved.parent else None,
            "directories": dirs,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# File viewer — browse and read files from the dashboard
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".rst", ".org",
    ".py", ".pyw", ".pyi", ".pyx",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".svg",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".cs", ".java", ".kt", ".kts",
    ".go", ".rs", ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".R",
    ".swift", ".m", ".mm", ".scala", ".clj", ".cljs", ".ex", ".exs",
    ".hs", ".ml", ".mli", ".erl", ".hrl", ".elm", ".nim", ".zig", ".v",
    ".sql", ".graphql", ".gql", ".proto",
    ".env", ".env.example", ".env.local",
    ".gitignore", ".gitattributes", ".gitmodules",
    ".dockerignore", ".editorconfig", ".prettierrc", ".eslintrc",
    ".log", ".csv", ".tsv",
    ".tf", ".tfvars", ".hcl",
    ".cmake", ".mk",
    ".patch", ".diff",
}

_TEXT_NAMES: set[str] = {
    "Makefile", "makefile", "GNUmakefile",
    "Dockerfile", "Containerfile",
    "Vagrantfile", "Procfile", "Gemfile", "Rakefile",
    "LICENSE", "LICENCE", "COPYING", "NOTICE",
    "README", "CHANGELOG", "CHANGES", "AUTHORS", "CONTRIBUTORS",
    "CLAUDE.md",
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig",
    "requirements.txt", "constraints.txt",
    "go.mod", "go.sum", "Cargo.lock",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "tsconfig.json",
    "Pipfile", "Pipfile.lock", "poetry.lock",
    "flake.nix", "flake.lock",
}

_PDF_EXTENSIONS: set[str] = {".pdf"}

_IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif",
}

_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".tiff": "image/tiff", ".tif": "image/tiff",
}

_MAX_FILE_BROWSE = 500
_MAX_TEXT_SIZE = 1 * 1024 * 1024      # 1 MB
_MAX_PDF_SIZE = 50 * 1024 * 1024      # 50 MB
_MAX_IMAGE_SIZE = 50 * 1024 * 1024    # 50 MB


def _classify_file(entry: Path) -> str:
    """Return 'text', 'pdf', 'image', or 'binary' for a file entry."""
    if entry.suffix.lower() in _PDF_EXTENSIONS:
        return "pdf"
    if entry.suffix.lower() in _IMAGE_EXTENSIONS:
        return "image"
    if entry.suffix.lower() in _TEXT_EXTENSIONS or entry.name in _TEXT_NAMES:
        return "text"
    # Heuristic: read first 8KB, check for null bytes + UTF-8 validity
    try:
        chunk = entry.read_bytes()[:8192]
        if b"\x00" in chunk:
            return "binary"
        chunk.decode("utf-8")
        return "text"
    except (OSError, UnicodeDecodeError):
        return "binary"


@router.get("/files/browse")
async def file_browse(path: str = "~", show_hidden: bool = False, root: str | None = None):
    """List directories and files in a path for the file viewer."""
    try:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="Not a directory")

        # Enforce root boundary — prevent browsing above the project root
        root_resolved = None
        if root:
            root_resolved = Path(root).expanduser().resolve()
            if not str(resolved).startswith(str(root_resolved)):
                resolved = root_resolved

        dirs: list[dict] = []
        files: list[dict] = []
        truncated = False
        count = 0

        try:
            for entry in sorted(resolved.iterdir(), key=lambda e: e.name.lower()):
                if not show_hidden and entry.name.startswith("."):
                    continue
                count += 1
                if count > _MAX_FILE_BROWSE:
                    truncated = True
                    break
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry), "type": "directory"})
                elif entry.is_file():
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    ftype = _classify_file(entry)
                    files.append({"name": entry.name, "path": str(entry), "size": size, "type": ftype})
        except PermissionError:
            pass

        # Hide parent link when at the root boundary
        show_parent = resolved != resolved.parent
        if root_resolved and resolved == root_resolved:
            show_parent = False

        return {
            "current": str(resolved),
            "parent": str(resolved.parent) if show_parent else None,
            "entries": dirs + files,
            "truncated": truncated,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/files/read")
async def file_read(path: str, request: Request, download: bool = False):
    """Read file content — text as JSON, PDF as binary stream."""
    from fastapi.responses import FileResponse as _FileResponse

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ftype = _classify_file(resolved)

    # Download mode — return raw file as attachment
    if download:
        media = "application/pdf" if ftype == "pdf" else "application/octet-stream"
        return _FileResponse(
            str(resolved),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'},
        )

    if ftype == "text":
        try:
            size = resolved.stat().st_size
        except OSError:
            raise HTTPException(status_code=400, detail="Cannot stat file")
        truncated = size > _MAX_TEXT_SIZE
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(_MAX_TEXT_SIZE)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        result: dict[str, Any] = {
            "content": content,
            "path": str(resolved),
            "size": min(size, _MAX_TEXT_SIZE),
            "truncated": truncated,
        }
        if truncated:
            result["total_size"] = size
        return result

    if ftype == "pdf":
        try:
            size = resolved.stat().st_size
        except OSError:
            raise HTTPException(status_code=400, detail="Cannot stat file")
        if size > _MAX_PDF_SIZE:
            raise HTTPException(status_code=413, detail=f"PDF too large ({size} bytes, max {_MAX_PDF_SIZE})")
        return _FileResponse(
            str(resolved),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{resolved.name}"'},
        )

    if ftype == "image":
        try:
            size = resolved.stat().st_size
        except OSError:
            raise HTTPException(status_code=400, detail="Cannot stat file")
        if size > _MAX_IMAGE_SIZE:
            raise HTTPException(status_code=413, detail=f"Image too large ({size} bytes, max {_MAX_IMAGE_SIZE})")
        mime = _IMAGE_MIME.get(resolved.suffix.lower(), "application/octet-stream")
        return _FileResponse(
            str(resolved),
            media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{resolved.name}"'},
        )

    raise HTTPException(status_code=415, detail="Unsupported file type")


def _require_admin(request: Request):
    """Raise 403 unless the request is from localhost or authenticated via token.

    When CONDUCTOR_TOKEN is set, the BearerAuthMiddleware already validated
    the token before we get here — so any request that reaches this point
    with a token configured is authenticated.  Without a token, only
    localhost is allowed (safe default).
    """
    if cfg.CONDUCTOR_TOKEN:
        return  # auth middleware already verified the bearer token
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="Admin settings require localhost access or a CONDUCTOR_TOKEN")


@router.get("/admin/settings")
async def get_admin_settings(request: Request):
    """Return full settings for the admin panel. Localhost only."""
    _require_admin(request)
    return cfg.get_admin_settings()


@router.put("/admin/settings")
async def put_admin_settings(request: Request):
    """Update settings and persist to ~/.be-conductor/config.yaml. Localhost only."""
    _require_admin(request)
    data = await request.json()
    cfg.save_user_config(data)
    return {"status": "ok", "config_version": cfg.get_config_version()}


@router.post("/admin/settings/reset")
async def reset_admin_settings(request: Request):
    """Reset all settings to built-in defaults. Localhost only."""
    _require_admin(request)
    cfg.reset_to_defaults()
    return {"status": "ok", "config_version": cfg.get_config_version()}


def _require_localhost(request: Request):
    """Raise 403 unless the request comes from localhost (regardless of token)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="This action requires localhost access")


@router.put("/admin/token")
async def set_admin_token(request: Request):
    """Set or update the auth token. Localhost only (even with token auth)."""
    _require_localhost(request)
    data = await request.json()
    token = data.get("token", "").strip()
    if not token or len(token) < 8:
        raise HTTPException(status_code=400, detail="Token must be at least 8 characters")
    if cfg._TOKEN_FROM_ENV:
        raise HTTPException(
            status_code=409,
            detail="Token is set via environment variable — remove BE_CONDUCTOR_TOKEN to manage via GUI",
        )
    cfg.set_conductor_token(token)
    return {"status": "ok", "auth_enabled": True}


@router.delete("/admin/token")
async def delete_admin_token(request: Request):
    """Remove the auth token. Localhost only."""
    _require_localhost(request)
    if cfg._TOKEN_FROM_ENV:
        raise HTTPException(
            status_code=409,
            detail="Token is set via environment variable — remove BE_CONDUCTOR_TOKEN to manage via GUI",
        )
    cfg.set_conductor_token(None)
    return {"status": "ok", "auth_enabled": False}


# ---------------------------------------------------------------------------
# SSL / HTTPS management (localhost-only)
# ---------------------------------------------------------------------------

def _get_local_ip() -> str:
    """Best-effort detection of the LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_log = logging.getLogger(__name__)


def _schedule_restart(delay: float = 1.0):
    """Spawn a new server process after *delay* seconds, then kill ourselves.

    The new process waits for the port to become free before binding.
    """
    async def _do_restart():
        await asyncio.sleep(delay)

        project_root = Path(__file__).parent.parent.parent.resolve()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")

        log_path = cfg.CONDUCTOR_DIR / "logs" / "server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a")

        popen_kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(project_root),
            env=env,
        )
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True

        cmd = [sys.executable, "-m", "be_conductor.server.app"]
        subprocess.Popen(cmd, **popen_kwargs)
        log.close()
        _log.info("Spawned new server process, shutting down current")

        # Kill ourselves — lifespan cleanup will run
        if sys.platform == "win32":
            os.kill(os.getpid(), signal.CTRL_C_EVENT)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    asyncio.ensure_future(_do_restart())


@router.post("/admin/ssl/generate")
async def generate_ssl_cert(request: Request):
    """Generate a self-signed certificate. Localhost only."""
    _require_localhost(request)

    cfg.CERTS_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = cfg.CERTS_DIR / "cert.pem"
    key_path = cfg.CERTS_DIR / "key.pem"

    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    days = data.get("days", 365)
    local_ip = _get_local_ip()
    san = f"DNS:localhost,IP:127.0.0.1,IP:{local_ip}"

    try:
        result = subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", str(days),
                "-nodes",
                "-subj", "/CN=be-conductor",
                "-addext", f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="openssl is not installed on the server")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"openssl error: {e.stderr}")

    key_path.chmod(0o600)
    cfg.set_ssl_config(str(cert_path), str(key_path))

    _schedule_restart()

    return {
        "status": "ok",
        "certfile": str(cert_path),
        "keyfile": str(key_path),
        "san": san,
        "days": days,
        "restarting": True,
        "new_url": f"https://127.0.0.1:{PORT}",
    }


@router.post("/admin/ssl/upload")
async def upload_ssl_cert(request: Request):
    """Upload certificate and key PEM content. Localhost only."""
    _require_localhost(request)

    data = await request.json()
    cert_pem = data.get("certfile", "").strip()
    key_pem = data.get("keyfile", "").strip()

    if not cert_pem or not key_pem:
        raise HTTPException(status_code=400, detail="Both certfile and keyfile are required")
    if not cert_pem.startswith("-----BEGIN"):
        raise HTTPException(status_code=400, detail="Certificate does not look like PEM format")
    if "KEY" not in key_pem[:50].upper():
        raise HTTPException(status_code=400, detail="Key does not look like PEM format")

    cfg.CERTS_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = cfg.CERTS_DIR / "cert.pem"
    key_path = cfg.CERTS_DIR / "key.pem"

    cert_path.write_text(cert_pem)
    key_path.write_text(key_pem)
    key_path.chmod(0o600)

    cfg.set_ssl_config(str(cert_path), str(key_path))

    _schedule_restart()

    return {"status": "ok", "restarting": True, "new_url": f"https://127.0.0.1:{PORT}"}


@router.delete("/admin/ssl")
async def remove_ssl_cert(request: Request):
    """Remove SSL configuration. Localhost only."""
    _require_localhost(request)

    cert_path = cfg.CERTS_DIR / "cert.pem"
    key_path = cfg.CERTS_DIR / "key.pem"
    cert_path.unlink(missing_ok=True)
    key_path.unlink(missing_ok=True)

    cfg.set_ssl_config(None, None)

    _schedule_restart()

    return {"status": "ok", "ssl_enabled": False, "restarting": True, "new_url": f"http://127.0.0.1:{PORT}"}


# ---------------------------------------------------------------------------
# Notification settings (accessible from all devices — not localhost-only)
# ---------------------------------------------------------------------------

@router.get("/notifications/settings")
async def get_notification_settings(request: Request):
    """Get notification settings for a device (identified by X-Device-Id header)."""
    device_id = request.headers.get("x-device-id", "")
    if not device_id:
        return {}
    return registry.notification_manager.get_device_settings(device_id)


@router.put("/notifications/settings")
async def put_notification_settings(request: Request):
    """Save notification settings for a device (browser/sound only)."""
    device_id = request.headers.get("x-device-id", "")
    if not device_id:
        raise HTTPException(status_code=400, detail="X-Device-Id header required")
    data = await request.json()
    # Only store per-device fields; webhook config is global
    device_data = {k: v for k, v in data.items()
                   if k in ("browser", "sound")}
    registry.notification_manager.set_device_settings(device_id, device_data)
    return {"status": "ok"}


@router.get("/notifications/webhook")
async def get_webhook_settings():
    """Get global webhook settings."""
    return registry.notification_manager.get_webhook_settings()


@router.put("/notifications/webhook")
async def put_webhook_settings(request: Request):
    """Save global webhook settings."""
    data = await request.json()
    registry.notification_manager.set_webhook_settings(data)
    return {"status": "ok"}


@router.post("/notifications/webhook/test")
async def test_notification_webhook(request: Request):
    """Send a test notification to verify webhook configuration."""
    data = await request.json()
    url = data.get("url", "")
    chat_id = data.get("chat_id")
    if not url:
        raise HTTPException(status_code=400, detail="Webhook URL required")
    ok, message = await test_webhook(url, chat_id=chat_id)
    return {"ok": ok, "message": message}


@router.get("/sessions")
async def list_sessions(request: Request):
    from starlette.responses import JSONResponse
    data = registry.list_all()
    resp = JSONResponse(data)
    resp.headers["X-Config-Version"] = str(cfg.get_config_version())
    return resp


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str):
    """Return a single session by ID (checks live and resumable)."""
    session = registry.get(session_id)
    if session:
        return session.to_dict()
    if session_id in registry.resumable:
        return registry.resumable[session_id]
    raise HTTPException(status_code=404, detail="Session not found")


@router.post("/sessions/run", response_model=SessionResponse)
async def create_session(req: RunRequest, request: Request):
    # Validate session name — no path traversal, shell metacharacters, etc.
    req.name = req.name.strip()
    if not _SAFE_NAME.match(req.name):
        raise HTTPException(
            status_code=400,
            detail="Invalid session name. Use letters, numbers, hyphens, underscores, or spaces (max 64 chars).",
        )

    # Validate command against whitelist (CLI is unrestricted, dashboard is restricted)
    if req.source != "cli":
        try:
            base_cmd = shlex.split(req.command)[0]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid command")

        allowed = _allowed_base_commands()
        if base_cmd not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Command '{base_cmd}' is not allowed. Permitted: {', '.join(sorted(allowed))}",
            )

    try:
        session = await registry.create(req.name, req.command, cwd=req.cwd, env=req.env,
                                        rows=req.rows, cols=req.cols, source=req.source,
                                        worktree=req.worktree)
        d = session.to_dict()
        d["ws_url"] = _ws_url_for(request, session.id)
        return d
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"Command not found: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/input", response_model=StatusResponse)
async def send_input(session_id: str, req: InputRequest):
    session = registry.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not req.text and not req.keys:
        raise HTTPException(status_code=400, detail="Provide at least one of 'text' or 'keys'")

    if req.text:
        session.send_input(req.text)

    if req.keys:
        for key_name in req.keys:
            seq = _KEY_MAP.get(key_name.upper())
            if seq is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown key: '{key_name}'. Supported: {', '.join(sorted(_KEY_MAP))}",
                )
            session.send_input(seq)

    return {"status": "ok"}


@router.post("/sessions/{session_id}/resize", response_model=StatusResponse)
async def resize_session(session_id: str, req: ResizeRequest):
    session = registry.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    old_rows, old_cols = session.rows, session.cols
    session.resize(req.rows, req.cols, source=req.source, client_id=req.client_id)
    # Notify browser clients of the new PTY size so they can refit.
    if session.rows != old_rows or session.cols != old_cols:
        owner = session.resize_owner_id or session.browser_resize_owner_id
        await _broadcast_resize(session_id, session.rows, session.cols,
                                session.resize_source, owner_id=owner)
    return {"status": "ok"}


@router.post("/sessions/{session_id}/upload", response_model=UploadResponse)
async def upload_file(session_id: str, request: Request):
    """Upload a file and return its path for use in the terminal."""
    session = registry.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()

    # Try to get a meaningful filename from Content-Disposition or X-Filename header,
    # otherwise generate one from content type and hash.
    original_name = request.headers.get("x-filename")
    if original_name:
        # Sanitize: keep only the basename, strip path separators
        original_name = original_name.replace("\\", "/").rsplit("/", 1)[-1]
        # Remove any non-safe characters
        safe_name = re.sub(r'[^\w.\-]', '_', original_name)
        hash4 = hashlib.sha256(body).hexdigest()[:4]
        filename = f"{int(time.time())}-{hash4}-{safe_name}"
    else:
        ext = _MIME_EXTENSIONS.get(content_type, "bin")
        hash8 = hashlib.sha256(body).hexdigest()[:8]
        filename = f"upload-{int(time.time())}-{hash8}.{ext}"

    upload_dir = UPLOADS_DIR / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / filename
    file_path.write_bytes(body)

    return {"path": str(file_path.resolve()), "filename": filename}


@router.post("/sessions/{session_id}/resume", response_model=SessionResponse)
async def resume_session(session_id: str, req: ResumeRequest | None = None):
    """Resume an exited session that has a stored --resume id."""
    try:
        rows = req.rows if req else None
        cols = req.cols if req else None
        session = await registry.resume(session_id, rows=rows, cols=cols)
        return session.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"Command not found: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/stop", response_model=StatusResponse)
async def stop_session(session_id: str, req: StopRequest | None = None):
    """Stop a session.

    Body (optional JSON):
      mode: "kill"     — hard stop, session is removed (default)
      mode: "graceful" — send SIGINT so the agent can print a resume token
      mode: "forget"   — send SIGINT but discard session without saving resume data
    """
    mode = (req.mode if req else None) or "kill"

    session = registry.get(session_id)
    if session:
        if mode == "graceful":
            registry.graceful_stop(session_id)
            return {"status": "stopping"}
        if mode == "forget":
            registry.forget(session_id)
            return {"status": "stopping"}
        await registry.remove(session_id)
        return {"status": "stopped"}

    # Session already exited and is kept for resume — don't dismiss it.
    # This prevents the CLI's stop-on-exit from accidentally deleting a
    # session that was saved for later.
    if session_id in registry.resumable:
        return {"status": "stopped"}

    raise HTTPException(status_code=404, detail="Session not found")


@router.delete("/sessions/{session_id}", response_model=StatusResponse)
async def kill_session(session_id: str):
    session = registry.get(session_id)
    if session:
        await registry.remove(session_id)
        notes_store.delete_by_session(session_id)
        return {"status": "killed"}

    # Maybe it's a resumable entry — dismiss it.
    if session_id in registry.resumable:
        registry.dismiss_resumable(session_id)
        notes_store.delete_by_session(session_id)
        return {"status": "dismissed"}

    raise HTTPException(status_code=404, detail="Session not found")


@router.delete("/sessions/resumable/all")
async def clear_all_resumable():
    """Remove all stopped resumable sessions (keeps worktree sessions)."""
    count = registry.clear_all_resumable()
    return {"status": "cleared", "count": count}


# ---------------------------------------------------------------------------
# Worktree endpoints
# ---------------------------------------------------------------------------

@router.get("/git/check")
async def git_check(path: str):
    """Check if a directory is a git repo (for dashboard worktree checkbox)."""
    from be_conductor.worktrees.manager import WorktreeManager
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, WorktreeManager.check_git_directory, path)
    return result


@router.get("/worktrees")
async def list_worktrees(repo: str | None = None):
    """List all managed worktrees, optionally filtered by repo path."""
    manager = registry.worktree_manager
    loop = asyncio.get_event_loop()
    worktrees = await loop.run_in_executor(None, manager.list_worktrees, repo)
    return [wt.to_dict() for wt in worktrees]


@router.get("/worktrees/health")
async def worktree_health():
    """Get health warnings for worktrees."""
    manager = registry.worktree_manager
    loop = asyncio.get_event_loop()
    warnings = await loop.run_in_executor(None, manager.get_warnings)
    return {"warnings": warnings, "count": len(warnings)}


@router.get("/worktrees/{name}")
async def get_worktree(name: str):
    """Get info for a specific worktree by session name."""
    manager = registry.worktree_manager
    worktrees = manager.list_worktrees()
    for wt in worktrees:
        if wt.name == name:
            return wt.to_dict()
    raise HTTPException(status_code=404, detail=f"Worktree '{name}' not found")


@router.get("/worktrees/{name}/diff")
async def get_worktree_diff(name: str, files: bool = False, format: str = "unified"):
    """Get the diff for a worktree vs its base commit.

    Query params:
        files:  If true, return file list with additions/deletions counts.
        format: "unified" (default) returns raw unified diff text.
                "rich" returns per-file base/head content pairs for IDE diff viewers.
    """
    manager = registry.worktree_manager
    worktrees = manager.list_worktrees()
    info = None
    for wt in worktrees:
        if wt.name == name:
            info = wt
            break
    if not info:
        raise HTTPException(status_code=404, detail=f"Worktree '{name}' not found")

    loop = asyncio.get_event_loop()

    if format == "rich":
        result = await loop.run_in_executor(None, manager.get_rich_diff, info)
        return {"files": result}

    result = await loop.run_in_executor(None, manager.get_diff, info, files)
    if files:
        return {"files": result}
    return {"diff": result}


@router.post("/worktrees/{name}/finalize")
async def finalize_worktree(name: str):
    """Explicitly finalize a worktree — auto-commit changes and mark as finalized."""
    # Must not be running — stop first
    session = registry.get(name)
    if session and session.status == "running":
        raise HTTPException(status_code=409, detail="Session is still running. Stop it first.")

    manager = registry.worktree_manager
    worktrees = manager.list_worktrees()
    info = None
    for wt in worktrees:
        if wt.name == name:
            info = wt
            break
    if not info:
        raise HTTPException(status_code=404, detail=f"Worktree '{name}' not found")

    if info.status == "finalized":
        return info.to_dict()

    loop = asyncio.get_event_loop()
    updated = await loop.run_in_executor(None, manager.finalize, info)

    # Update the resumable metadata with finalized worktree
    if name in registry.resumable:
        registry.resumable[name]["worktree"] = updated.to_dict()
        registry._save_metadata_dict(registry.resumable[name])

    return updated.to_dict()


class MergeRequest(BaseModel):
    strategy: str = "squash"
    message: str | None = None


class GCRequest(BaseModel):
    dry_run: bool = False
    max_age_days: float = 7.0


@router.post("/worktrees/{name}/merge/preview")
async def preview_merge(name: str):
    """Preview what merging a worktree would do."""
    manager = registry.worktree_manager
    worktrees = manager.list_worktrees()
    info = None
    for wt in worktrees:
        if wt.name == name:
            info = wt
            break
    if not info:
        raise HTTPException(status_code=404, detail=f"Worktree '{name}' not found")

    loop = asyncio.get_event_loop()
    preview = await loop.run_in_executor(None, manager.preview_merge, info)
    return {
        "can_merge": preview.can_merge,
        "commits_ahead": preview.commits_ahead,
        "commits_behind": preview.commits_behind,
        "conflict_files": preview.conflict_files,
        "changed_files": preview.changed_files,
        "message": preview.message,
    }


@router.post("/worktrees/{name}/merge")
async def merge_worktree(name: str, req: MergeRequest):
    """Merge a worktree branch back into its base branch."""
    manager = registry.worktree_manager
    worktrees = manager.list_worktrees()
    info = None
    for wt in worktrees:
        if wt.name == name:
            info = wt
            break
    if not info:
        raise HTTPException(status_code=404, detail=f"Worktree '{name}' not found")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, manager.merge, info, req.strategy, req.message
    )
    # Sync updated worktree info (base_commit, commits_ahead) into resumable
    if result.success and name in registry.resumable:
        meta = registry.resumable[name]
        if meta.get("worktree"):
            meta["worktree"]["base_commit"] = info.base_commit
            meta["worktree"]["commits_ahead"] = 0
    return {
        "success": result.success,
        "strategy": result.strategy,
        "merged_branch": result.merged_branch,
        "target_branch": result.target_branch,
        "commits_merged": result.commits_merged,
        "conflict_files": result.conflict_files,
        "message": result.message,
    }


@router.delete("/worktrees/{name}")
async def delete_worktree(name: str, force: bool = False):
    """Delete a worktree and its branch."""
    manager = registry.worktree_manager
    worktrees = manager.list_worktrees()
    info = None
    for wt in worktrees:
        if wt.name == name:
            info = wt
            break
    if not info:
        raise HTTPException(status_code=404, detail=f"Worktree '{name}' not found")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, manager.remove, info, force)
        registry.dismiss_resumable(name)
        return {"status": "removed", "name": name}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/worktrees/gc")
async def worktree_gc(req: GCRequest):
    """Garbage-collect stale worktrees."""
    manager = registry.worktree_manager
    loop = asyncio.get_event_loop()
    actions = await loop.run_in_executor(
        None, manager.gc, req.max_age_days, req.dry_run
    )
    return actions


# ---------------------------------------------------------------------------
# External Claude Code sessions — discovery, resume, and observation
# ---------------------------------------------------------------------------

# file_id: either a bare UUID (backward compat → claude) or agent::id.
# agent prefix is one of: claude, codex, copilot, gemini, goose.
# The raw_id part is a UUID or similar safe identifier.
_VALID_AGENTS = {"claude", "codex", "copilot", "gemini", "goose"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SAFE_FILE_ID_RE = re.compile(r"^(?:(?:claude|codex|copilot|gemini|goose)::)?[a-zA-Z0-9._-]{1,200}$")


def _validate_file_id(file_id: str):
    """Raise 400 if file_id is not a valid agent::id or bare UUID."""
    if not _SAFE_FILE_ID_RE.match(file_id):
        raise HTTPException(status_code=400, detail="Invalid session file ID")
    # If prefixed, validate the agent name
    if "::" in file_id:
        agent = file_id.split("::", 1)[0]
        if agent not in _VALID_AGENTS:
            raise HTTPException(status_code=400, detail="Unknown agent prefix")


def _conductor_resume_ids() -> set[str]:
    """Collect file_ids already managed by Be-Conductor (to exclude from scan).

    Returns both bare IDs and claude:: prefixed forms for backward compat.
    """
    ids = set()
    _resume_re = re.compile(r'--resume\s+(\S+)')
    # Running sessions — check command for --resume <file_id>
    for session in registry.sessions.values():
        m = _resume_re.search(session.command)
        if m:
            raw_id = m.group(1)
            ids.add(raw_id)
            # Also add prefixed form so new-style file_ids match
            if "::" not in raw_id:
                ids.add(f"claude::{raw_id}")
    # Resumable (exited) sessions — resume_id IS the file_id
    for meta in registry.resumable.values():
        rid = meta.get("resume_id")
        if rid and rid != "__always__":
            ids.add(rid)
            if "::" not in rid:
                ids.add(f"claude::{rid}")
    return ids


@router.get("/external/sessions")
async def list_external_sessions(project: str | None = None, agent: str | None = None):
    """Discover external AI agent sessions."""
    loop = asyncio.get_event_loop()
    conductor_ids = _conductor_resume_ids()
    results = await loop.run_in_executor(
        None, _external_scanner.scan, project, conductor_ids, agent
    )
    return results


class ExternalResumeRequest(BaseModel):
    name: str


@router.post("/external/sessions/{file_id}/resume")
async def resume_external_session(file_id: str, req: ExternalResumeRequest, request: Request):
    """Resume an external agent session in a Be-Conductor PTY."""
    _validate_file_id(file_id)
    req.name = req.name.strip()
    if not _SAFE_NAME.match(req.name):
        raise HTTPException(
            status_code=400,
            detail="Invalid session name.",
        )

    # Look up session info from scanner
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _external_scanner.get_session_info, file_id)
    if not info:
        raise HTTPException(status_code=404, detail="External session not found")

    # Use the agent-specific resume command
    command = info.get("resume_command")
    if not command:
        raise HTTPException(status_code=400, detail="No resume command for this session")
    cwd = info.get("cwd")

    try:
        session = await registry.create(req.name, command, cwd=cwd)
        d = session.to_dict()
        d["ws_url"] = _ws_url_for(request, session.id)
        _external_scanner.invalidate()
        return d
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"Command not found: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.websocket("/external/sessions/{file_id}/observe")
async def observe_external_session(ws: WebSocket, file_id: str):
    """WebSocket endpoint for read-only observation of a running agent session."""
    if not _SAFE_FILE_ID_RE.match(file_id):
        await ws.close(code=4003, reason="Invalid session file ID")
        return
    if not _check_ws_auth(ws):
        await ws.close(code=4001, reason="Unauthorized")
        return

    # Determine agent from file_id prefix
    from be_conductor.external.scanner import _parse_file_id
    agent, _raw_id = _parse_file_id(file_id)

    # Find the JSONL file
    loop = asyncio.get_event_loop()
    jsonl_path = await loop.run_in_executor(None, _external_scanner.get_jsonl_path, file_id)
    if not jsonl_path:
        await ws.close(code=4004, reason="Session not observable")
        return

    await ws.accept()

    # Get or create observer for this file
    observer = _observers.get(file_id)
    if not observer:
        observer = SessionObserver(jsonl_path, agent=agent)
        _observers[file_id] = observer
        await observer.start()

    # Send history buffer
    buffer = observer.get_buffer()
    if buffer:
        await ws.send_bytes(buffer)

    queue = observer.subscribe()

    async def writer():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    if data is None:
                        break
                    await ws.send_bytes(data)
                except asyncio.TimeoutError:
                    # Keepalive ping
                    try:
                        await ws.send_bytes(b"")
                    except Exception:
                        break
        except Exception:
            pass

    async def reader():
        """Consume client messages but ignore them (read-only)."""
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                # Ignore all input — this is read-only
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())

    try:
        done, pending = await asyncio.wait(
            {writer_task, reader_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        observer.unsubscribe(queue)
        # If no more subscribers, stop and remove observer
        if observer.subscriber_count == 0:
            await observer.stop()
            _observers.pop(file_id, None)


# ---------------------------------------------------------------------------
# WebSocket — supports typed=true mode for agent clients
# ---------------------------------------------------------------------------

@router.websocket("/sessions/{session_id}/stream")
async def stream_session(ws: WebSocket, session_id: str, typed: bool = False,
                         source: str | None = None, client_id: str | None = None):
    # Auth check
    if not _check_ws_auth(ws):
        await ws.close(code=4001, reason="Unauthorized")
        return

    session = registry.get(session_id)
    if not session:
        await ws.close(code=4004, reason="Session not found")
        return

    await ws.accept()

    # Track CLI/browser connections for resize authority
    is_cli = source == "cli" and client_id
    is_browser = source == "browser" and client_id
    if is_cli:
        session.cli_connected(client_id)
    elif is_browser:
        session.browser_connected(client_id)

    # Track this WebSocket for notification broadcast.
    _notification_ws[ws] = session_id

    try:
        if typed:
            await _stream_typed(ws, session)
        else:
            await _stream_raw(ws, session)
    finally:
        _notification_ws.pop(ws, None)
        if is_cli:
            session.cli_disconnected(client_id)
        elif is_browser:
            session.browser_disconnected(client_id)


async def _stream_raw(ws: WebSocket, session: Any):
    """Original raw binary WebSocket protocol (dashboard default)."""
    # Send buffered output first
    buffer = session.get_buffer()
    if buffer:
        await ws.send_bytes(buffer)
    wm = session._watermark_seq()
    if wm:
        await ws.send_bytes(wm)

    queue = session.subscribe()

    async def writer():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    if data is None:
                        # Session ended — close WebSocket
                        await ws.close()
                        break
                    # Coalesce: drain any additional pending chunks so
                    # rapid TUI updates (spinners, cursor moves) arrive
                    # as one atomic write instead of many tiny ones.
                    chunks = [data]
                    while not queue.empty():
                        more = queue.get_nowait()
                        if more is None:
                            await ws.send_bytes(b"".join(chunks))
                            await ws.close()
                            return
                        chunks.append(more)
                    await ws.send_bytes(b"".join(chunks) if len(chunks) > 1 else data)
                except asyncio.TimeoutError:
                    # Keepalive
                    try:
                        await ws.send_bytes(b"")
                    except Exception:
                        break
        except Exception:
            pass

    async def reader():
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                raw = message.get("bytes")
                if text:
                    # Intercept notification ack (browser saw the notification)
                    try:
                        ctrl = json.loads(text)
                        if isinstance(ctrl, dict) and ctrl.get("type") == "notification_ack":
                            _notification_ack.set()
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                    session.send_input(text)
                elif raw:
                    session.send_input_bytes(raw)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())

    try:
        done, pending = await asyncio.wait(
            {writer_task, reader_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        session.unsubscribe(queue)


async def _stream_typed(ws: WebSocket, session: Any):
    """Typed JSON WebSocket protocol for agent clients."""
    # Send buffered output as a typed stdout message
    buffer = session.get_buffer()
    if buffer:
        await ws.send_json({"type": "stdout", "data": buffer.decode("utf-8", errors="replace")})
    wm = session._watermark_seq()
    if wm:
        await ws.send_json({"type": "stdout", "data": wm.decode("utf-8", errors="replace")})

    queue = session.subscribe()

    async def writer():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    if data is None:
                        await ws.send_json({
                            "type": "exit",
                            "exit_code": session.exit_code,
                        })
                        await ws.close()
                        break
                    # Coalesce pending chunks for atomic delivery
                    chunks = [data]
                    while not queue.empty():
                        more = queue.get_nowait()
                        if more is None:
                            merged = b"".join(chunks)
                            await ws.send_json({
                                "type": "stdout",
                                "data": merged.decode("utf-8", errors="replace"),
                            })
                            await ws.send_json({
                                "type": "exit",
                                "exit_code": session.exit_code,
                            })
                            await ws.close()
                            return
                        chunks.append(more)
                    merged = b"".join(chunks) if len(chunks) > 1 else data
                    await ws.send_json({
                        "type": "stdout",
                        "data": merged.decode("utf-8", errors="replace"),
                    })
                except asyncio.TimeoutError:
                    try:
                        await ws.send_json({"type": "ping"})
                    except Exception:
                        break
        except Exception:
            pass

    async def reader():
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break

                text = message.get("text")
                raw = message.get("bytes")

                if text:
                    # Try parsing as JSON typed message
                    try:
                        msg = json.loads(text)
                        msg_type = msg.get("type")
                        if msg_type == "input":
                            session.send_input(msg.get("data", ""))
                        elif msg_type == "resize":
                            rows = msg.get("rows", 24)
                            cols = msg.get("cols", 80)
                            session.resize(rows, cols,
                                           source=msg.get("source"),
                                           client_id=msg.get("client_id"))
                    except (json.JSONDecodeError, TypeError):
                        # Plain text fallback — treat as raw input
                        session.send_input(text)
                elif raw:
                    session.send_input_bytes(raw)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())

    try:
        done, pending = await asyncio.wait(
            {writer_task, reader_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        session.unsubscribe(queue)


# ---------------------------------------------------------------------------
# Notes endpoints
# ---------------------------------------------------------------------------

class NoteCreate(BaseModel):
    content: str
    scope: str = "global"
    project_id: str | None = None
    session_id: str | None = None


class NoteUpdate(BaseModel):
    content: str | None = None
    scope: str | None = None
    project_id: str | None = None
    session_id: str | None = None


class NoteReorder(BaseModel):
    order: list[str]


async def _broadcast_notes_event(
    action: str,
    note: dict | None = None,
    note_id: str | None = None,
):
    """Push note changes to all connected WebSocket clients."""
    msg = json.dumps({"type": "notes", "action": action, "note": note, "noteId": note_id})
    dead: list[WebSocket] = []
    for ws in list(_notification_ws):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _notification_ws.pop(ws, None)


@router.get("/notes")
async def list_notes(
    scope: str | None = None,
    projectId: str | None = None,
    sessionId: str | None = None,
    q: str | None = None,
):
    notes = notes_store.list_notes(
        scope=scope, project_id=projectId, session_id=sessionId, q=q,
    )
    return {"notes": notes}


@router.post("/notes")
async def create_note(body: NoteCreate):
    note = notes_store.create_note(
        content=body.content,
        scope=body.scope,
        project_id=body.project_id,
        session_id=body.session_id,
    )
    await _broadcast_notes_event("created", note=note)
    return note


@router.patch("/notes/reorder")
async def reorder_notes(body: NoteReorder):
    notes_store.reorder(body.order)
    await _broadcast_notes_event("reordered")
    return {"status": "ok"}


@router.patch("/notes/{note_id}")
async def update_note(note_id: str, body: NoteUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    note = notes_store.update_note(note_id, **fields)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await _broadcast_notes_event("updated", note=note)
    return note


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str):
    if not notes_store.delete_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    await _broadcast_notes_event("deleted", note_id=note_id)
    return {"status": "deleted"}


@router.delete("/notes")
async def bulk_delete_notes(
    sessionId: str | None = None,
    projectId: str | None = None,
    scope: str | None = None,
    confirm: bool = False,
):
    if sessionId:
        count = notes_store.delete_by_session(sessionId)
    elif projectId:
        count = notes_store.delete_by_project(projectId)
    elif confirm:
        count = notes_store.delete_all()
    else:
        raise HTTPException(status_code=400, detail="Pass confirm=true to delete all notes")
    await _broadcast_notes_event("cleared")
    return {"status": "cleared", "count": count}


@router.get("/notes/export")
async def export_notes(
    scope: str | None = None,
    projectId: str | None = None,
):
    from fastapi.responses import PlainTextResponse
    md = notes_export.export_markdown(scope=scope, project_id=projectId)
    return PlainTextResponse(md, media_type="text/markdown")
