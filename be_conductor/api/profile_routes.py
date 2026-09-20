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

"""REST routes for account profiles, headless task results and MCP settings."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from be_conductor.api.routes import _require_admin, _ws_url_for, registry
from be_conductor.profiles import (
    ProfileError, get_profile, ledger, list_profiles, profile_status,
    save_profiles, secrets,
)
from be_conductor.sessions import tasks
from be_conductor.utils import config as cfg

router = APIRouter()


def _profile_or_404(name: str) -> dict:
    try:
        return get_profile(name)
    except ProfileError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Profiles ──────────────────────────────────────────────────────────────

@router.get("/profiles")
async def get_profiles():
    """List profiles with status. Never includes secret values."""
    return {
        "profiles": [profile_status(p) for p in list_profiles()],
        "secret_backend": secrets.backend_name(),
    }


@router.put("/admin/profiles")
async def put_profiles(request: Request):
    """Replace the profile list. Localhost or token only."""
    _require_admin(request)
    data = await request.json()
    profiles = data.get("profiles") if isinstance(data, dict) else data
    if not isinstance(profiles, list):
        raise HTTPException(status_code=400, detail="expected a list of profiles")
    try:
        saved = save_profiles(profiles)
    except ProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "profiles": saved,
            "config_version": cfg.get_config_version()}


class SecretRequest(BaseModel):
    env: str
    value: str


@router.post("/admin/profiles/{name}/secret")
async def set_profile_secret(name: str, req: SecretRequest, request: Request):
    """Store a secret in the keyring. The value is never returned."""
    _require_admin(request)
    profile = _profile_or_404(name)
    if not req.value:
        raise HTTPException(status_code=400, detail="empty value")
    ref = next((s["keyring"] for s in profile["secrets"] if s["env"] == req.env), None)
    if ref is None:
        # Declare the secret on the profile so it is injected from now on.
        ref = f"be-conductor/{name}-{req.env}"
        raw = [dict(p) for p in cfg.PROFILES]
        for p in raw:
            if p.get("name") == name:
                p["secrets"] = list(p.get("secrets") or []) + [{"env": req.env, "keyring": ref}]
        try:
            save_profiles(raw)
        except ProfileError as e:
            raise HTTPException(status_code=400, detail=str(e))
    backend = secrets.set_secret(ref, req.value)
    return {"status": "ok", "backend": backend}


@router.delete("/admin/profiles/{name}/secret/{env}")
async def delete_profile_secret(name: str, env: str, request: Request):
    _require_admin(request)
    profile = _profile_or_404(name)
    ref = next((s["keyring"] for s in profile["secrets"] if s["env"] == env), None)
    if ref is None:
        raise HTTPException(status_code=404, detail=f"profile '{name}' has no secret {env}")
    return {"status": "ok", "removed": secrets.delete_secret(ref)}


class LeanCtxRequest(BaseModel):
    mode: str


@router.post("/admin/profiles/{name}/leanctx")
async def set_profile_leanctx(name: str, req: LeanCtxRequest, request: Request):
    """Set up / remove LeanCTX inside the profile's config dir only."""
    import asyncio
    from be_conductor.profiles import leanctx
    _require_admin(request)
    _profile_or_404(name)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, leanctx.set_mode, name, req.mode)
    except ProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))


class LoginRequest(BaseModel):
    rows: int | None = None
    cols: int | None = None


@router.post("/profiles/{name}/login")
async def login_profile(name: str, request: Request, req: LoginRequest | None = None):
    """Start a PTY session running the backend's login flow under the profile."""
    _profile_or_404(name)
    try:
        session = await tasks.start_login(
            registry, name, rows=req.rows if req else None, cols=req.cols if req else None)
    except (ProfileError, ValueError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"Command not found: {e}")
    d = session.to_dict()
    d["ws_url"] = _ws_url_for(request, session.id)
    return d


@router.post("/profiles/{name}/check")
async def check_profile(name: str):
    """Run a trivial headless prompt under the profile (60 s limit)."""
    _profile_or_404(name)
    try:
        return await tasks.check_profile(registry, name)
    except (ProfileError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"Command not found: {e}"}


@router.get("/profiles/{name}/usage")
async def profile_usage(name: str):
    _profile_or_404(name)
    return ledger.usage(name)


# ── Headless task results ─────────────────────────────────────────────────

@router.get("/sessions/{session_id}/result")
async def session_result(session_id: str, tail: int = 0):
    """Task status and result for a headless run."""
    record = registry.task_result(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No headless run with that id")
    if tail and record.get("task_status") in ("running", "needs_input"):
        session = registry.get(session_id)
        if session is not None:
            record = {**record, "output_tail": session.get_buffer_text(max_lines=tail)}
    return record


# ── MCP settings ──────────────────────────────────────────────────────────

@router.get("/admin/mcp")
async def get_mcp_settings(request: Request):
    _require_admin(request)
    from be_conductor.mcp_server import install as mcp_install
    from be_conductor.mcp_server.server import exposed_entries, bridge_status
    return {
        "config": cfg.MCP_CONFIG,
        "enabled": bool(cfg.MCP_CONFIG.get("enabled")),
        "exposed": [e.get("label") or e["command"] for e in exposed_entries()],
        "claude_desktop": mcp_install.status(),
        "snippet": mcp_install.snippet(),
        "bridge": bridge_status(),
    }


@router.put("/admin/mcp")
async def put_mcp_settings(request: Request):
    _require_admin(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="expected an object")
    clean: dict = {"enabled": bool(data.get("enabled"))}
    for key in ("expose_commands", "allowed_dirs"):
        val = data.get(key) or []
        if not isinstance(val, list):
            raise HTTPException(status_code=400, detail=f"{key} must be a list")
        clean[key] = [str(v) for v in val]
    timeout = data.get("default_timeout_seconds") or 900
    try:
        clean["default_timeout_seconds"] = max(10, int(timeout))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="default_timeout_seconds must be a number")
    # `servers` (named remotes for the bridge) is config-file only — keep it.
    if cfg.MCP_CONFIG.get("servers"):
        clean["servers"] = cfg.MCP_CONFIG["servers"]
    cfg.MCP_CONFIG = clean
    cfg.save_user_config({"mcp": clean})
    return {"status": "ok", "config": clean}


@router.post("/admin/mcp/install")
async def install_mcp(request: Request):
    _require_admin(request)
    from be_conductor.mcp_server import install as mcp_install
    try:
        return {"status": "ok", **mcp_install.install()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/mcp/uninstall")
async def uninstall_mcp(request: Request):
    _require_admin(request)
    from be_conductor.mcp_server import install as mcp_install
    try:
        return {"status": "ok", **mcp_install.uninstall()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
