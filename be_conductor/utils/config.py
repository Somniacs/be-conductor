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

"""Central configuration — networking, paths, buffer sizes, command whitelist."""

import os
from importlib.metadata import version as _pkg_version
from pathlib import Path

import yaml

try:
    VERSION = _pkg_version("be-conductor")
except Exception:
    VERSION = "0.0.0"
CONDUCTOR_TOKEN = os.environ.get("BE_CONDUCTOR_TOKEN") or os.environ.get("CONDUCTOR_TOKEN")

HOST = "0.0.0.0"
PORT = 7777
BASE_URL = f"http://127.0.0.1:{PORT}"

CONDUCTOR_DIR = Path.home() / ".be-conductor"
SESSIONS_DIR = CONDUCTOR_DIR / "sessions"
LOG_DIR = CONDUCTOR_DIR / "logs"
UPLOADS_DIR = CONDUCTOR_DIR / "uploads"
PID_FILE = CONDUCTOR_DIR / "server.pid"
USER_CONFIG_FILE = CONDUCTOR_DIR / "config.yaml"
WORKTREES_FILE = CONDUCTOR_DIR / "worktrees.json"

# ── Defaults (overridden by ~/.be-conductor/config.yaml if it exists) ───────

BUFFER_MAX_BYTES = 1_000_000  # 1MB rolling buffer
UPLOAD_WARN_SIZE = 20 * 1024 * 1024  # 20 MB — frontend shows confirmation above this
GRACEFUL_STOP_TIMEOUT = 30  # seconds before force-kill
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}

_DEFAULT_ALLOWED_COMMANDS = [
    {
        "command": "claude",
        "label": "Claude Code",
        "resume_pattern": r"--resume\s+(\S+)",
        "resume_flag": "--resume",
        "stop_sequence": ["\x03", "/exit", "\r"],
    },
    {
        "command": "claude --dangerously-skip-permissions",
        "label": "Claude Code (skip permissions)",
        "resume_pattern": r"--resume\s+(\S+)",
        "resume_flag": "--resume",
        "stop_sequence": ["\x03", "/exit", "\r"],
    },
    {
        "command": "codex",
        "label": "OpenAI Codex CLI",
        "resume_command": "codex resume",
        "stop_sequence": ["\x03"],
    },
    {
        "command": "codex --full-auto",
        "label": "OpenAI Codex CLI (full auto)",
        "resume_command": "codex resume --last",
        "stop_sequence": ["\x03"],
    },
    {
        "command": "copilot",
        "label": "GitHub Copilot CLI",
        "resume_command": "copilot --resume",
    },
    {
        "command": "copilot --allow-all-tools",
        "label": "GitHub Copilot CLI (allow all)",
        "resume_command": "copilot --continue",
    },
    {
        "command": "gemini",
        "label": "Gemini CLI",
        "resume_command": "gemini --resume",
        "stop_sequence": ["\x03"],
    },
    {
        "command": "opencode",
        "label": "OpenCode",
        "resume_command": "opencode --continue",
        "stop_sequence": ["\x03"],
    },
    {
        "command": "amp",
        "label": "Amp (Sourcegraph)",
        "stop_sequence": ["\x03"],
    },
    {"command": "aider", "label": "Aider"},
    {
        "command": "goose",
        "label": "Goose (Block)",
        "resume_command": "goose session --resume",
        "stop_sequence": ["\x03"],
    },
    {"command": "forge", "label": "Forge"},
    {"command": "cursor", "label": "Cursor Agent"},
]

_DEFAULT_DIRECTORIES = [
    str(Path.home()),
    str(Path.home() / "Documents"),
]

# ── Mutable runtime state ───────────────────────────────────────────────────

ALLOWED_COMMANDS: list[dict] = list(_DEFAULT_ALLOWED_COMMANDS)
DEFAULT_DIRECTORIES: list[str] = list(_DEFAULT_DIRECTORIES)

_config_version: int = 0


def get_config_version() -> int:
    return _config_version


def migrate_from_old_name():
    """Detect ~/.conductor and migrate to ~/.be-conductor on first run."""
    import logging
    _log = logging.getLogger(__name__)

    old_dir = Path.home() / ".conductor"
    new_dir = CONDUCTOR_DIR  # ~/.be-conductor

    if old_dir.exists() and not new_dir.exists():
        _log.info("Migrating data directory: %s -> %s", old_dir, new_dir)
        old_dir.rename(new_dir)
        _log.info("Migration complete.")
    elif old_dir.exists() and new_dir.exists():
        _log.warning(
            "Both %s and %s exist. Using %s. "
            "Merge or remove the old directory manually.",
            old_dir, new_dir, new_dir,
        )

    if os.environ.get("CONDUCTOR_TOKEN") and not os.environ.get("BE_CONDUCTOR_TOKEN"):
        _log.warning(
            "CONDUCTOR_TOKEN is deprecated. "
            "Rename to BE_CONDUCTOR_TOKEN in your environment/service file."
        )


def load_user_config():
    """Load ~/.be-conductor/config.yaml and merge over defaults."""
    global ALLOWED_COMMANDS, DEFAULT_DIRECTORIES, BUFFER_MAX_BYTES, UPLOAD_WARN_SIZE, GRACEFUL_STOP_TIMEOUT

    if not USER_CONFIG_FILE.exists():
        return

    try:
        data = yaml.safe_load(USER_CONFIG_FILE.read_text()) or {}
    except Exception:
        return

    if "allowed_commands" in data and isinstance(data["allowed_commands"], list):
        ALLOWED_COMMANDS = data["allowed_commands"]
    if "default_directories" in data and isinstance(data["default_directories"], list):
        DEFAULT_DIRECTORIES = data["default_directories"]
    if "buffer_max_bytes" in data and isinstance(data["buffer_max_bytes"], int):
        BUFFER_MAX_BYTES = data["buffer_max_bytes"]
    if "upload_warn_size" in data and isinstance(data["upload_warn_size"], int):
        UPLOAD_WARN_SIZE = data["upload_warn_size"]
    if "graceful_stop_timeout" in data and isinstance(data["graceful_stop_timeout"], (int, float)):
        GRACEFUL_STOP_TIMEOUT = data["graceful_stop_timeout"]


def save_user_config(data: dict):
    """Write settings to ~/.be-conductor/config.yaml and update in-memory values."""
    global ALLOWED_COMMANDS, DEFAULT_DIRECTORIES, BUFFER_MAX_BYTES, UPLOAD_WARN_SIZE, GRACEFUL_STOP_TIMEOUT, _config_version

    if "allowed_commands" in data and isinstance(data["allowed_commands"], list):
        ALLOWED_COMMANDS = data["allowed_commands"]
    if "default_directories" in data and isinstance(data["default_directories"], list):
        DEFAULT_DIRECTORIES = data["default_directories"]
    if "buffer_max_bytes" in data and isinstance(data["buffer_max_bytes"], int):
        BUFFER_MAX_BYTES = data["buffer_max_bytes"]
    if "upload_warn_size" in data and isinstance(data["upload_warn_size"], int):
        UPLOAD_WARN_SIZE = data["upload_warn_size"]
    if "graceful_stop_timeout" in data and isinstance(data["graceful_stop_timeout"], (int, float)):
        GRACEFUL_STOP_TIMEOUT = data["graceful_stop_timeout"]

    config_out = {
        "allowed_commands": ALLOWED_COMMANDS,
        "default_directories": DEFAULT_DIRECTORIES,
        "buffer_max_bytes": BUFFER_MAX_BYTES,
        "upload_warn_size": UPLOAD_WARN_SIZE,
        "graceful_stop_timeout": GRACEFUL_STOP_TIMEOUT,
    }

    CONDUCTOR_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_FILE.write_text(yaml.dump(config_out, default_flow_style=False, sort_keys=False))
    _config_version += 1


def get_editable_settings() -> dict:
    """Return current editable settings for the admin API."""
    return {
        "allowed_commands": ALLOWED_COMMANDS,
        "default_directories": DEFAULT_DIRECTORIES,
        "buffer_max_bytes": BUFFER_MAX_BYTES,
        "upload_warn_size": UPLOAD_WARN_SIZE,
        "graceful_stop_timeout": GRACEFUL_STOP_TIMEOUT,
    }


def reset_to_defaults():
    """Reset all settings to built-in defaults and remove config.yaml."""
    global ALLOWED_COMMANDS, DEFAULT_DIRECTORIES, BUFFER_MAX_BYTES, UPLOAD_WARN_SIZE, GRACEFUL_STOP_TIMEOUT, _config_version

    ALLOWED_COMMANDS = list(_DEFAULT_ALLOWED_COMMANDS)
    DEFAULT_DIRECTORIES = list(_DEFAULT_DIRECTORIES)
    BUFFER_MAX_BYTES = 1_000_000
    UPLOAD_WARN_SIZE = 20 * 1024 * 1024
    GRACEFUL_STOP_TIMEOUT = 30

    if USER_CONFIG_FILE.exists():
        USER_CONFIG_FILE.unlink()
    _config_version += 1


def get_admin_settings() -> dict:
    """Return full settings for the admin panel (editable + read-only)."""
    return {
        **get_editable_settings(),
        "host": HOST,
        "port": PORT,
        "version": VERSION,
        "auth_enabled": CONDUCTOR_TOKEN is not None,
    }


# ── Migrate + load user config on import ────────────────────────────────────

migrate_from_old_name()
load_user_config()


def ensure_dirs():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
