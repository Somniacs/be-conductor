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

"""Task and profile operations shared by the REST API and the MCP tools."""

import os
import re
import shlex
import uuid
from pathlib import Path

from be_conductor.profiles import (
    BACKEND_CLI, ProfileError, get_profile, ledger,
)
from be_conductor.sessions import headless as hl
from be_conductor.utils import config as cfg

# Backend → the command a login session runs.  All three print a URL or
# device code, so the flow can be finished from the dashboard on a phone.
LOGIN_COMMANDS = {
    "claude": "claude auth login",
    "codex": "codex login --device-auth",
    "opencode": "opencode auth login",
}


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:max_len].strip("_") or "agent"


def task_name(label: str) -> str:
    return f"task-{slugify(label, 32).replace('_', '-')}-{uuid.uuid4().hex[:6]}"


def resolve_entry(command_ref: str, profile: str | None = None) -> dict:
    entry = hl.find_command(command_ref, profile)
    if not entry:
        raise hl.HeadlessError(
            f"'{command_ref}' is not a configured command (use a label or command "
            "from allowed_commands)")
    return entry


def check_allowed_dir(path: str | None) -> str:
    """Resolve *path* and require it to sit inside ``mcp.allowed_dirs``."""
    allowed = [Path(os.path.expanduser(str(d))).resolve()
               for d in (cfg.MCP_CONFIG.get("allowed_dirs") or [])]
    if not allowed:
        raise PermissionError(
            "no working directories are allowed — set mcp.allowed_dirs in "
            "~/.be-conductor/config.yaml")
    if not path:
        raise PermissionError("working_dir is required")
    resolved = Path(os.path.expanduser(path)).resolve()
    if not resolved.is_dir():
        raise PermissionError(f"working_dir does not exist: {resolved}")
    for root in allowed:
        if resolved == root or root in resolved.parents:
            return str(resolved)
    raise PermissionError(
        f"working_dir {resolved} is outside mcp.allowed_dirs "
        f"({', '.join(str(a) for a in allowed)})")


async def start_task(registry, command_ref: str, prompt: str, *,
                     cwd: str | None = None, profile: str | None = None,
                     worktree: bool = False, timeout_seconds: float | None = None,
                     name: str | None = None, model: str | None = None):
    """Start a headless run. Returns the live HeadlessSession."""
    if not prompt or not prompt.strip():
        raise hl.HeadlessError("prompt is empty")
    entry = resolve_entry(command_ref, profile)
    if not hl.headless_block(entry):
        raise hl.HeadlessError(
            f"command '{entry.get('label') or entry['command']}' has no headless block")
    profile = profile or entry.get("profile") or None
    if timeout_seconds is None:
        timeout_seconds = cfg.MCP_CONFIG.get("default_timeout_seconds") or 900
    return await registry.create(
        name or task_name(entry.get("label") or entry["command"]),
        entry["command"],
        cwd=cwd,
        worktree=worktree,
        profile=profile,
        headless={"entry": entry, "prompt": prompt,
                  "timeout_seconds": timeout_seconds,
                  "model": (model or "").strip() or None},
    )


async def start_login(registry, profile_name: str,
                      rows: int | None = None, cols: int | None = None):
    """Start an interactive PTY session that logs the profile in."""
    profile = get_profile(profile_name)
    command = profile.get("login_command") or LOGIN_COMMANDS.get(profile["backend"])
    if not command:
        raise ProfileError(
            f"profile '{profile_name}' ({profile['backend']}) has no login command")
    return await registry.create(
        f"login-{profile_name}", command, cwd=str(Path.home()),
        rows=rows, cols=cols, profile=profile_name)


async def check_profile(registry, profile_name: str, timeout: float = 60) -> dict:
    """Run the backend's trivial headless prompt under the profile."""
    profile = get_profile(profile_name)
    cli = BACKEND_CLI.get(profile["backend"])
    entry = None
    for e in cfg.ALLOWED_COMMANDS:
        if isinstance(e, dict) and e.get("profile") == profile_name and hl.headless_block(e):
            entry = e
            break
    if entry is None:
        if not cli:
            raise ProfileError(
                f"profile '{profile_name}' is a custom profile — bind it to a "
                "command with a headless block to check it")
        entry = {"command": cli, "label": cli, "headless": True}

    session = await registry.create(
        f"check-{profile_name}-{uuid.uuid4().hex[:4]}", entry["command"],
        cwd=str(Path.home()), profile=profile_name,
        headless={"entry": entry, "prompt": hl.CHECK_PROMPT, "timeout_seconds": timeout},
    )
    # A check is plumbing, not work the user wants to keep in the sidebar.
    session._forget = True
    if not await session.wait(timeout + 15):
        await registry.remove(session.id)
        registry.dismiss_resumable(session.id)
    record = session.task_record()
    hl.delete_task_record(session.id)
    ok = record["task_status"] == "done"
    detail = (record.get("result") or "")[:500]
    ledger.record_check(profile_name, ok, detail if not ok else "")
    return {"ok": ok, "task_status": record["task_status"],
            "fail_reason": record.get("fail_reason"),
            "result": detail, "cost_usd": record.get("cost_usd"),
            "duration_s": record.get("duration_s")}


def models_for_entry(entry: dict) -> list[str]:
    """Models an exposed command can run: its profile's real list when it has
    one, else the backend's catalogue."""
    from be_conductor.profiles.manager import catalog_models
    from be_conductor.profiles import list_models
    profile = entry.get("profile")
    if profile:
        try:
            return list_models(profile)
        except ProfileError:
            return []
    exe = os.path.basename(shlex.split(entry.get("command") or "")[0]) if entry.get("command") else ""
    backend = next((b for b, cli in BACKEND_CLI.items() if cli == exe), None)
    return catalog_models(backend, None) if backend else []


def format_footer(record: dict, dashboard_url: str | None = None) -> str:
    parts = [f"session={record.get('name') or record.get('id')}"]
    if record.get("profile"):
        parts.append(f"profile={record['profile']}")
    if record.get("model"):
        parts.append(f"model={record['model']}")
    status = record.get("task_status") or "unknown"
    if record.get("fail_reason") and record["fail_reason"] != status:
        status += f"({record['fail_reason']})"
    parts.append(f"status={status}")
    if record.get("duration_s") is not None:
        parts.append(f"duration={record['duration_s']:.0f}s")
    if record.get("cost_usd") is not None:
        parts.append(f"cost=${record['cost_usd']:.4f}")
    if dashboard_url:
        parts.append(f"dashboard={dashboard_url}")
    return "[" + " ".join(parts) + "]"
