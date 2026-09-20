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

"""Register the stdio bridge in Claude Desktop's claude_desktop_config.json."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_KEY = "be-conductor"


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


def status() -> dict:
    path = config_path()
    out = {"config_path": str(path), "claude_desktop_found": config_dir().is_dir(),
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


def install() -> dict:
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


def uninstall() -> dict:
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
