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

"""Register the stdio bridge with Claude.

Two places read MCP servers, and neither reads the other's list:

* **desktop** — Claude Desktop's chat side: ``claude_desktop_config.json``.
* **code** — Claude Code, which is also what the Code tab of Claude Desktop
  runs: its own user-scope list in ``~/.claude.json``.  That file is rewritten
  all the time by running sessions, so writes go through the ``claude`` CLI
  instead of editing it here; only the status check reads it.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_KEY = "be-conductor"
TARGETS = ("desktop", "code")


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"


def config_path() -> Path:
    return config_dir() / "claude_desktop_config.json"


def bridge_command() -> str:
    """Absolute path of the be-conductor executable Claude Desktop should launch."""
    found = shutil.which("be-conductor")
    if found:
        return str(Path(found).resolve()) if sys.platform != "win32" else found
    exe = Path(sys.argv[0])
    if exe.name.startswith("be-conductor") and exe.exists():
        return str(exe.resolve())
    # Same venv as the running interpreter.
    cand = Path(sys.executable).parent / ("be-conductor.exe" if sys.platform == "win32" else "be-conductor")
    return str(cand) if cand.exists() else "be-conductor"


def server_entry() -> dict:
    return {"command": bridge_command(), "args": ["mcp"]}


def snippet() -> str:
    return json.dumps({"mcpServers": {SERVER_KEY: server_entry()}}, indent=2)


def _load() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)   # a corrupt file is the user's to fix — don't clobber it
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _backup() -> str | None:
    path = config_path()
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    return str(backup)


def _desktop_status() -> dict:
    path = config_path()
    out = {"config_path": str(path), "found": config_dir().is_dir(),
           "installed": False, "entry": None}
    try:
        entry = (_load().get("mcpServers") or {}).get(SERVER_KEY)
    except Exception as e:
        out["error"] = str(e)
        return out
    out["installed"] = entry is not None
    out["entry"] = entry
    out["current"] = entry == server_entry()
    return out


def _desktop_install() -> dict:
    data = _load()
    backup = _backup()
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers in claude_desktop_config.json is not an object")
    servers[SERVER_KEY] = server_entry()
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"config_path": str(path), "backup": backup, "entry": servers[SERVER_KEY]}


def _desktop_uninstall() -> dict:
    path = config_path()
    data = _load()
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return {"config_path": str(path), "backup": None, "removed": False}
    backup = _backup()
    del servers[SERVER_KEY]
    if not servers:
        del data["mcpServers"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"config_path": str(path), "backup": backup, "removed": True}


# ── Claude Code (CLI and the Code tab of Claude Desktop) ──────────────────

def _claude_code_config() -> Path:
    return Path.home() / ".claude.json"


def _claude_env() -> dict:
    """Environment for the `claude` CLI: the user's *default* Claude Code
    config, never a profile's — and none of the variables a parent Claude
    session leaves behind."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def _code_status() -> dict:
    exe = shutil.which("claude")
    path = _claude_code_config()
    out = {"config_path": str(path), "found": bool(exe), "installed": False, "entry": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        entry = (data.get("mcpServers") or {}).get(SERVER_KEY)
    except Exception as e:
        out["error"] = str(e)
        return out
    out["installed"] = entry is not None
    out["entry"] = entry
    want = server_entry()
    out["current"] = bool(entry) and entry.get("command") == want["command"] \
        and list(entry.get("args") or []) == want["args"]
    return out


def _claude(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("the `claude` CLI is not on PATH")
    return subprocess.run([exe, "mcp", *args], env=_claude_env(), capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)


def _code_install() -> dict:
    before = _code_status()
    if before.get("installed") and before.get("current"):
        return {"config_path": before["config_path"], "entry": before["entry"], "changed": False}
    if before.get("installed"):
        _claude("remove", SERVER_KEY, "-s", "user")   # stale command path — replace it
    entry = server_entry()
    proc = _claude("add", "--scope", "user", SERVER_KEY, "--", entry["command"], *entry["args"])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "claude mcp add failed").strip()[-400:])
    return {"config_path": before["config_path"], "entry": entry, "changed": True}


def _code_uninstall() -> dict:
    before = _code_status()
    if not before.get("installed"):
        return {"config_path": before["config_path"], "removed": False}
    proc = _claude("remove", SERVER_KEY, "-s", "user")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "claude mcp remove failed").strip()[-400:])
    return {"config_path": before["config_path"], "removed": True}


# ── Both ──────────────────────────────────────────────────────────────────

_IMPL = {
    "desktop": (_desktop_status, _desktop_install, _desktop_uninstall),
    "code": (_code_status, _code_install, _code_uninstall),
}
LABELS = {"desktop": "Claude Desktop (chat)", "code": "Claude Code (CLI and the Code tab)"}


def _targets(target: str) -> list[str]:
    if target == "both":
        return list(TARGETS)
    if target not in TARGETS:
        raise ValueError(f"target must be one of: {', '.join(TARGETS)}, both")
    return [target]


def status() -> dict:
    return {t: _IMPL[t][0]() for t in TARGETS}


def _apply(index: int, target: str) -> dict:
    """Run install (1) or uninstall (2) on each target.  With "both", a
    target that is not present on this machine is skipped, not an error."""
    out: dict = {}
    for t in _targets(target):
        if target == "both" and not _IMPL[t][0]().get("found") and index == 1:
            out[t] = {"skipped": f"{LABELS[t]} not found on this machine"}
            continue
        try:
            out[t] = _IMPL[t][index]()
        except Exception as e:
            out[t] = {"error": str(e)}
    return out


def install(target: str = "both") -> dict:
    return _apply(1, target)


def uninstall(target: str = "both") -> dict:
    return _apply(2, target)
