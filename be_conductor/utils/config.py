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
_TOKEN_FROM_ENV = CONDUCTOR_TOKEN is not None  # True if token came from environment

HOST = "0.0.0.0"
PORT = 7777

CONDUCTOR_DIR = Path.home() / ".be-conductor"
SESSIONS_DIR = CONDUCTOR_DIR / "sessions"
LOG_DIR = CONDUCTOR_DIR / "logs"
UPLOADS_DIR = CONDUCTOR_DIR / "uploads"
CERTS_DIR = CONDUCTOR_DIR / "certs"
PID_FILE = CONDUCTOR_DIR / "server.pid"
TOKEN_FILE = CONDUCTOR_DIR / "token"
USER_CONFIG_FILE = CONDUCTOR_DIR / "config.yaml"
WORKTREES_FILE = CONDUCTOR_DIR / "worktrees.json"
NOTES_DB = CONDUCTOR_DIR / "notes.db"

# ── SSL / TLS ────────────────────────────────────────────────────────────────

SSL_CERTFILE: str | None = os.environ.get("BE_CONDUCTOR_SSL_CERTFILE")
SSL_KEYFILE: str | None = os.environ.get("BE_CONDUCTOR_SSL_KEYFILE")


def get_base_url() -> str:
    scheme = "https" if SSL_CERTFILE else "http"
    return f"{scheme}://127.0.0.1:{PORT}"

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
    global SSL_CERTFILE, SSL_KEYFILE

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
    # SSL — env vars take precedence over config file
    if not SSL_CERTFILE and "ssl_certfile" in data and isinstance(data["ssl_certfile"], str):
        SSL_CERTFILE = data["ssl_certfile"]
    if not SSL_KEYFILE and "ssl_keyfile" in data and isinstance(data["ssl_keyfile"], str):
        SSL_KEYFILE = data["ssl_keyfile"]


def save_user_config(data: dict):
    """Write settings to ~/.be-conductor/config.yaml and update in-memory values."""
    global ALLOWED_COMMANDS, DEFAULT_DIRECTORIES, BUFFER_MAX_BYTES, UPLOAD_WARN_SIZE, GRACEFUL_STOP_TIMEOUT, _config_version
    global SSL_CERTFILE, SSL_KEYFILE

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
    if "ssl_certfile" in data:
        SSL_CERTFILE = data["ssl_certfile"] or None
    if "ssl_keyfile" in data:
        SSL_KEYFILE = data["ssl_keyfile"] or None

    config_out = {
        "allowed_commands": ALLOWED_COMMANDS,
        "default_directories": DEFAULT_DIRECTORIES,
        "buffer_max_bytes": BUFFER_MAX_BYTES,
        "upload_warn_size": UPLOAD_WARN_SIZE,
        "graceful_stop_timeout": GRACEFUL_STOP_TIMEOUT,
    }
    if SSL_CERTFILE:
        config_out["ssl_certfile"] = SSL_CERTFILE
    if SSL_KEYFILE:
        config_out["ssl_keyfile"] = SSL_KEYFILE

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
    global SSL_CERTFILE, SSL_KEYFILE

    ALLOWED_COMMANDS = list(_DEFAULT_ALLOWED_COMMANDS)
    DEFAULT_DIRECTORIES = list(_DEFAULT_DIRECTORIES)
    BUFFER_MAX_BYTES = 1_000_000
    UPLOAD_WARN_SIZE = 20 * 1024 * 1024
    GRACEFUL_STOP_TIMEOUT = 30
    SSL_CERTFILE = None
    SSL_KEYFILE = None

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
        "auth_from_env": _TOKEN_FROM_ENV,
        "ssl_enabled": SSL_CERTFILE is not None and SSL_KEYFILE is not None,
        "ssl_certfile": SSL_CERTFILE,
        "ssl_keyfile": SSL_KEYFILE,
    }


# ── Token management ────────────────────────────────────────────────────────

def _load_stored_token():
    """Load token from ~/.be-conductor/token if env var isn't set."""
    global CONDUCTOR_TOKEN
    if CONDUCTOR_TOKEN:
        return  # env var takes precedence
    if TOKEN_FILE.exists():
        try:
            stored = TOKEN_FILE.read_text().strip()
            if stored:
                CONDUCTOR_TOKEN = stored
        except Exception:
            pass


def set_conductor_token(token: str | None):
    """Set or clear the auth token. Persists to disk and updates in-memory."""
    global CONDUCTOR_TOKEN
    CONDUCTOR_DIR.mkdir(parents=True, exist_ok=True)
    if token:
        TOKEN_FILE.write_text(token)
        TOKEN_FILE.chmod(0o600)
        CONDUCTOR_TOKEN = token
    else:
        TOKEN_FILE.unlink(missing_ok=True)
        CONDUCTOR_TOKEN = None


# ── Migrate + load user config on import ────────────────────────────────────

migrate_from_old_name()
_load_stored_token()
load_user_config()


def set_ssl_config(certfile: str | None, keyfile: str | None):
    """Set or clear SSL cert/key paths. Persists to config.yaml."""
    global SSL_CERTFILE, SSL_KEYFILE
    SSL_CERTFILE = certfile
    SSL_KEYFILE = keyfile
    save_user_config({"ssl_certfile": certfile, "ssl_keyfile": keyfile})


def ensure_dirs():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
