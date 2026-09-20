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

"""Profile model, validation and the session environment builder.

A profile is a named environment bundle: it points an agent CLI at its own
config directory (so it logs in separately from the machine's default
account), strips inherited auth variables, and injects secrets from the
keyring at spawn time.
"""

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from be_conductor.profiles import secrets
from be_conductor.utils import config as cfg

# Backend → the env var(s) that relocate its config. Paths are relative to
# the profile's config_dir ("" = the dir itself).
#
# OpenCode keeps its config under XDG_CONFIG_HOME but its *credentials*
# (auth.json) under XDG_DATA_HOME — both must move or the profile would
# still see the default login.
BACKEND_ENV: dict[str, dict[str, str]] = {
    "claude": {"CLAUDE_CONFIG_DIR": ""},
    "codex": {"CODEX_HOME": ""},
    "opencode": {"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "data"},
    "custom": {},
}

# Backend → the CLI a login / check session runs.
BACKEND_CLI: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
}

# Backend → how it is named in a generated command label.
BACKEND_DISPLAY: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
}

DEFAULT_STRIP_ENV = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
]

LEANCTX_MODES = ("off", "shadow", "active")

_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProfileError(ValueError):
    """A profile is invalid or cannot be applied."""


@dataclass
class SessionEnv:
    """What a profile contributes to a spawned process."""
    env: dict[str, str] = field(default_factory=dict)   # variables to set
    strip: list[str] = field(default_factory=list)      # variables to remove first
    redact: list[str] = field(default_factory=list)     # secret values to scrub from output


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _protected_dirs() -> list[Path]:
    home = Path.home()
    return [home, home / ".claude", home / ".codex", home / ".config",
            home / ".local" / "share"]


def default_config_dir(name: str) -> Path:
    return cfg.PROFILES_DIR / name


def config_dir_of(profile: dict) -> Path:
    raw = profile.get("config_dir")
    return _expand(raw) if raw else default_config_dir(profile["name"])


def validate_profile(profile: dict) -> dict:
    """Return a normalized copy of *profile* or raise ProfileError."""
    if not isinstance(profile, dict):
        raise ProfileError("profile must be a mapping")
    name = str(profile.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ProfileError(
            f"invalid profile name {name!r} — use lowercase letters, digits, '-' and '_'")
    backend = str(profile.get("backend") or "custom")
    if backend not in BACKEND_ENV:
        raise ProfileError(
            f"profile '{name}': unknown backend {backend!r} "
            f"(expected one of {', '.join(BACKEND_ENV)})")

    out: dict = {"name": name, "backend": backend}

    if backend != "custom" or profile.get("config_dir"):
        cdir = config_dir_of({**profile, "name": name})
        if not profile.get("allow_default_dir"):
            resolved = cdir.resolve()
            for prot in _protected_dirs():
                if resolved == prot.resolve():
                    raise ProfileError(
                        f"profile '{name}': config_dir {cdir} is a default config "
                        "location — a profile must use its own directory "
                        "(set allow_default_dir: true to override)")
        out["config_dir"] = str(profile.get("config_dir") or cdir)
    if profile.get("allow_default_dir"):
        out["allow_default_dir"] = True

    env = profile.get("env") or {}
    if not isinstance(env, dict):
        raise ProfileError(f"profile '{name}': env must be a mapping")
    for k in env:
        if not _ENV_NAME_RE.match(str(k)):
            raise ProfileError(f"profile '{name}': invalid env name {k!r}")
    out["env"] = {str(k): str(v) for k, v in env.items()}

    sec_out = []
    for s in profile.get("secrets") or []:
        if not isinstance(s, dict) or not s.get("env"):
            raise ProfileError(f"profile '{name}': each secret needs an 'env' name")
        if not _ENV_NAME_RE.match(str(s["env"])):
            raise ProfileError(f"profile '{name}': invalid secret env name {s['env']!r}")
        if "value" in s:
            raise ProfileError(
                f"profile '{name}': secret values do not belong in config — "
                "store them with `be-conductor profile set-key`")
        sec_out.append({
            "env": str(s["env"]),
            "keyring": str(s.get("keyring") or f"be-conductor/{name}-{s['env']}"),
        })
    out["secrets"] = sec_out

    strip = profile.get("strip_env")
    if strip is None:
        strip = list(DEFAULT_STRIP_ENV)
    if not isinstance(strip, list):
        raise ProfileError(f"profile '{name}': strip_env must be a list")
    out["strip_env"] = [str(x) for x in strip]

    leanctx = str(profile.get("leanctx") or "off")
    # YAML reads a bare `off` as False.
    if profile.get("leanctx") is False:
        leanctx = "off"
    if leanctx not in LEANCTX_MODES:
        raise ProfileError(f"profile '{name}': leanctx must be one of {', '.join(LEANCTX_MODES)}")
    out["leanctx"] = leanctx

    cap = profile.get("max_cost_usd_per_run")
    if cap is not None:
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            raise ProfileError(f"profile '{name}': max_cost_usd_per_run must be a number")
        if cap <= 0:
            raise ProfileError(f"profile '{name}': max_cost_usd_per_run must be > 0")
    out["max_cost_usd_per_run"] = cap

    if profile.get("model"):
        out["model"] = str(profile["model"])
    if profile.get("description"):
        out["description"] = str(profile["description"])
    return out


def command_template(profile: dict) -> dict | None:
    """A ready-made ``allowed_commands`` entry for this profile.

    The resume and stop settings come from the CLI's own built-in defaults,
    so a profile-bound command behaves exactly like the stock one — the user
    only has to confirm it instead of retyping what the profile already says.
    """
    cli = BACKEND_CLI.get(profile["backend"])
    if not cli:
        return None
    base: dict = {}
    for entry in cfg._DEFAULT_ALLOWED_COMMANDS:
        # The plain entry, not a variant like "claude --dangerously-skip-permissions".
        if entry.get("command") == cli:
            base = entry
            break

    name = profile["name"]
    suffix = name
    for prefix in (profile["backend"] + "-", profile["backend"] + "_"):
        if suffix.startswith(prefix):
            suffix = suffix[len(prefix):]
            break
    display = BACKEND_DISPLAY.get(profile["backend"], cli)
    pretty = suffix.replace("-", " ").replace("_", " ").title()
    out = {k: v for k, v in base.items() if k != "label"}
    out["command"] = out.get("command") or cli
    out["label"] = f"{display} — {pretty}" if pretty else display
    out["profile"] = name
    out["headless"] = True
    return out


def list_profiles() -> list[dict]:
    """All valid profiles from config (invalid entries are skipped)."""
    out, seen = [], set()
    for raw in cfg.PROFILES:
        try:
            p = validate_profile(raw)
        except ProfileError:
            continue
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        out.append(p)
    return out


def get_profile(name: str) -> dict:
    for raw in cfg.PROFILES:
        if isinstance(raw, dict) and raw.get("name") == name:
            return validate_profile(raw)   # surfaces *why* it is invalid
    raise ProfileError(f"unknown profile '{name}'")


def save_profiles(profiles: list[dict]) -> list[dict]:
    """Validate and persist the full profile list."""
    clean, seen = [], set()
    for raw in profiles:
        p = validate_profile(raw)
        if p["name"] in seen:
            raise ProfileError(f"duplicate profile name '{p['name']}'")
        seen.add(p["name"])
        clean.append(p)
    cfg.PROFILES = clean
    cfg.save_user_config({"profiles": clean})
    return clean


def ensure_config_dir(profile: dict) -> Path | None:
    if "config_dir" not in profile:
        return None
    cdir = config_dir_of(profile)
    cdir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            cdir.chmod(0o700)
        except OSError:
            pass
    for sub in BACKEND_ENV[profile["backend"]].values():
        if sub:
            (cdir / sub).mkdir(parents=True, exist_ok=True)
    return cdir


def build_session_env(profile_name: str | None,
                     extra_env: dict | None = None,
                     require_secrets: bool = True) -> SessionEnv:
    """Resolve a profile into the env changes for a spawned session.

    Session env = os.environ − strip_env + backend var + env + secrets,
    then *extra_env* on top.  Secrets are read from the keyring here, at
    spawn time; a declared secret that is missing is an error so a keyed
    profile never silently runs on some other credential.

    *require_secrets* is False for the login session itself — that is the
    flow that obtains credentials, so demanding them first is circular.
    """
    if not profile_name:
        return SessionEnv(env=dict(extra_env or {}))

    profile = get_profile(profile_name)
    out = SessionEnv(strip=list(profile["strip_env"]))

    cdir = ensure_config_dir(profile)
    if cdir is not None:
        for var, sub in BACKEND_ENV[profile["backend"]].items():
            out.env[var] = str(cdir / sub) if sub else str(cdir)

    out.env.update(profile["env"])

    for s in profile["secrets"]:
        value = secrets.get_secret(s["keyring"])
        if not value and not require_secrets:
            continue
        if not value:
            login_hint = (" Or drop this variable from the profile and press "
                          "'Login' to sign it in the way you would in a terminal."
                          if profile["backend"] in BACKEND_CLI else "")
            raise ProfileError(
                f"profile '{profile_name}': secret {s['env']} is not set — "
                f"use the 'Set API key' button in Settings → Profiles, or run "
                f"`be-conductor profile set-key {profile_name} {s['env']}`."
                + login_hint)
        out.env[s["env"]] = value
        out.redact.append(value)

    if extra_env:
        out.env.update({str(k): str(v) for k, v in extra_env.items()})
    # A variable the profile sets on purpose must survive the strip pass.
    out.strip = [k for k in out.strip if k not in out.env]
    return out


def profiles_for_command(command: str) -> list[dict]:
    """Profiles whose backend matches the command's executable."""
    import shlex
    try:
        base = os.path.basename(shlex.split(command)[0])
    except (ValueError, IndexError):
        return []
    return [p for p in list_profiles()
            if p["backend"] == "custom" or BACKEND_CLI.get(p["backend"]) == base]


def profile_status(profile: dict) -> dict:
    """API view of a profile: config plus status, never secret values."""
    from be_conductor.profiles import ledger
    cdir = config_dir_of(profile) if "config_dir" in profile else None
    logged_in = None
    if cdir is not None:
        marker = {
            "claude": cdir / ".credentials.json",
            "codex": cdir / "auth.json",
            "opencode": cdir / "data" / "opencode" / "auth.json",
        }.get(profile["backend"])
        if marker is not None:
            logged_in = marker.is_file()
    usage = ledger.usage(profile["name"])
    return {
        **profile,
        "command_template": command_template(profile),
        "config_dir_exists": bool(cdir and cdir.is_dir()),
        "logged_in": logged_in,
        "secrets": [
            {**s, "set": secrets.has_secret(s["keyring"])} for s in profile["secrets"]
        ],
        "secret_backend": secrets.backend_name(),
        "cli_found": _cli_found(profile),
        "usage_today_usd": usage["today"]["cost_usd"],
        "usage_week_usd": usage["week"]["cost_usd"],
        "last_check": ledger.last_check(profile["name"]),
    }


def _cli_found(profile: dict) -> bool | None:
    import shutil
    cli = BACKEND_CLI.get(profile["backend"])
    return bool(shutil.which(cli)) if cli else None


# Backend → how to list the models a profile can actually use. The command
# runs with the profile's environment, so what comes back reflects that
# account's providers (OpenCode lists nothing for a provider it has no key
# for). Claude Code has no list command; it takes these aliases.
MODEL_LIST_COMMAND: dict[str, list[str]] = {
    "opencode": ["opencode", "models"],
}

# Codex has no list command, but caches what the account may use, inside the
# profile's own CODEX_HOME — so it is already account-accurate.
CODEX_MODEL_CACHE = "models_cache.json"
STATIC_MODELS: dict[str, list[str]] = {
    "claude": ["opus", "sonnet", "haiku", "opusplan", "default"],
}

_MODEL_CACHE: dict[str, tuple[float, list[str]]] = {}
_MODEL_CACHE_TTL = 300


# OpenCode's model catalogue, cached by OpenCode itself. Readable without
# credentials, so a profile that has no key yet can still offer a model list.
OPENCODE_CATALOG = Path.home() / ".cache" / "opencode" / "models.json"


def catalog_models(backend: str, env_names: list[str] | None = None) -> list[str]:
    """Models a backend could use, before any key is stored.

    *env_names* (the API-key variables the profile declares) narrows the
    catalogue to the providers those keys unlock — the whole catalogue is
    ~7900 models, one provider is a usable list.
    """
    import json
    if backend in STATIC_MODELS:
        return list(STATIC_MODELS[backend])
    if backend != "opencode" or not OPENCODE_CATALOG.is_file():
        return []
    try:
        catalog = json.loads(OPENCODE_CATALOG.read_text())
    except (OSError, ValueError):
        return []
    wanted = {e.strip().upper() for e in (env_names or []) if e.strip()}
    out: list[str] = []
    for prov_id, prov in catalog.items():
        if not isinstance(prov, dict):
            continue
        if wanted and not wanted & {str(e).upper() for e in (prov.get("env") or [])}:
            continue
        out.extend(f"{prov_id}/{m}" for m in (prov.get("models") or {}))
    return sorted(out)


def _codex_models(profile: dict) -> list[str]:
    """Models this Codex login may use, from the cache Codex keeps itself.

    Written after the first run, so it is empty until the profile has been
    used once. Hidden entries (internal / special-purpose) are left out.
    """
    import json
    path = config_dir_of(profile) / CODEX_MODEL_CACHE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    models = [m for m in (data.get("models") or [])
              if isinstance(m, dict) and m.get("slug")
              and str(m.get("visibility", "list")).lower() != "hide"]
    models.sort(key=lambda m: m.get("priority") if isinstance(m.get("priority"), int) else 9999)
    return [m["slug"] for m in models]


def list_models(profile_name: str, refresh: bool = False) -> list[str]:
    """Model ids available to this profile, newest listing cached briefly."""
    import subprocess
    import time

    profile = get_profile(profile_name)
    backend = profile["backend"]
    if backend in STATIC_MODELS:
        return list(STATIC_MODELS[backend])
    if backend == "codex":
        return _codex_models(profile)
    argv = MODEL_LIST_COMMAND.get(backend)
    if not argv:
        return []

    hit = _MODEL_CACHE.get(profile_name)
    if hit and not refresh and time.time() - hit[0] < _MODEL_CACHE_TTL:
        return hit[1]

    senv = build_session_env(profile_name)
    env = {k: v for k, v in os.environ.items() if k not in senv.strip}
    env.update(senv.env)
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=30, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    models = [line.strip() for line in (proc.stdout or "").splitlines()
              if line.strip() and "/" in line and not line.startswith(" ")]
    _MODEL_CACHE[profile_name] = (time.time(), models)
    return models
