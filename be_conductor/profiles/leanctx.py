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

"""Optional LeanCTX (context compression) setup, scoped to one profile.

``lean-ctx init --agent <backend>`` is run with the profile's environment so
it installs into the profile's config dir.  LeanCTX documents no way to
target a custom dir, so nothing is taken on trust:

* the profile dir is snapshotted first, and ``off`` undoes exactly what
  ``init`` changed — no ``lean-ctx uninstall``, which is machine-wide;
* the *default* agent config files are snapshotted too, and restored at once
  if ``init`` wrote to them anyway.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from be_conductor.profiles.manager import (
    BACKEND_ENV, ProfileError, build_session_env, config_dir_of, get_profile,
)
from be_conductor.utils import config as cfg

_AGENT_NAME = {"claude": "claude", "codex": "codex", "opencode": "opencode"}

# Session data / caches — large, and never what an init touches.
_SKIP_DIRS = {"projects", "todos", "shell-snapshots", "statsig", "data", "sessions",
              "log", "logs", "cache", "node_modules", "leanctx-backup", "file-history"}
_MAX_BACKUP_BYTES = 512 * 1024


def _default_sentinels() -> list[Path]:
    home = Path.home()
    return [
        home / ".claude.json", home / ".claude" / "CLAUDE.md",
        home / ".claude" / "settings.json", home / ".claude" / "skills",
        home / ".codex" / "config.toml", home / ".codex" / "AGENTS.md",
        home / ".config" / "opencode" / "opencode.json",
        home / ".config" / "opencode" / "opencode.jsonc",
        home / ".config" / "opencode" / "AGENTS.md",
    ]


def _walk(root: Path) -> dict[str, tuple[float, int]]:
    out: dict[str, tuple[float, int]] = {}
    if not root.exists():
        return out
    if root.is_file():
        st = root.stat()
        return {"": (st.st_mtime, st.st_size)}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_mtime, st.st_size)
    return out


def _mentions_leanctx(path: Path) -> bool:
    """True if the file (by name or content) is LeanCTX's doing.

    The default config files are live — a running agent rewrites e.g.
    ~/.claude.json all the time — so a changed mtime alone proves nothing,
    and restoring on it would clobber somebody else's legitimate write.
    """
    if "lean" in path.name.lower() or "lean-ctx" in str(path.parent).lower():
        return True
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return False
        text = path.read_text(errors="ignore").lower()
    except OSError:
        return False
    return any(m in text for m in ("lean-ctx", "leanctx", "lean_ctx"))


def _backup_dir(profile: dict) -> Path:
    return cfg.PROFILES_DIR / profile["name"] / "leanctx-backup"


def _copy_tree_state(root: Path, state: dict, dest: Path):
    """Copy every (small) file in *state* under *dest*, mirroring paths."""
    for rel, (_mt, size) in state.items():
        if size > _MAX_BACKUP_BYTES:
            continue
        src = root / rel if rel else root
        target = dest / (rel or root.name)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        except OSError:
            pass


def _set_mode(name: str, mode: str):
    raw = [dict(p) for p in cfg.PROFILES]
    for p in raw:
        if p.get("name") == name:
            p["leanctx"] = mode
    from be_conductor.profiles.manager import save_profiles
    save_profiles(raw)


def set_mode(name: str, mode: str) -> dict:
    if mode not in ("off", "shadow", "active"):
        raise ProfileError("mode must be off, shadow or active")
    profile = get_profile(name)
    agent = _AGENT_NAME.get(profile["backend"])
    if not agent:
        raise ProfileError("LeanCTX needs a claude, codex or opencode profile")
    return _disable(profile) if mode == "off" else _enable(profile, agent, mode)


def _enable(profile: dict, agent: str, mode: str) -> dict:
    exe = shutil.which("lean-ctx")
    if not exe:
        raise ProfileError(
            "lean-ctx is not installed (https://leanctx.com) — be-conductor never installs it for you")
    cdir = config_dir_of(profile)
    senv = build_session_env(profile["name"])
    env = os.environ.copy()
    for key in senv.strip:
        env.pop(key, None)
    env.update(senv.env)

    backup = _backup_dir(profile)
    manifest_path = backup / "manifest.json"
    # Keep the oldest snapshot: re-running init must not turn LeanCTX's own
    # files into the "original" state that `off` restores.
    fresh = not manifest_path.is_file()
    before = _walk(cdir)
    if fresh:
        shutil.rmtree(backup, ignore_errors=True)
        _copy_tree_state(cdir, before, backup / "profile")

    sentinels = _default_sentinels()
    guard = backup / "defaults-guard"
    shutil.rmtree(guard, ignore_errors=True)
    default_before = {}
    for i, s in enumerate(sentinels):
        state = _walk(s)
        default_before[i] = state
        _copy_tree_state(s, state, guard / str(i))

    try:
        proc = subprocess.run([exe, "init", "--agent", agent], env=env, cwd=str(cdir),
                              capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ProfileError("lean-ctx init timed out")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()[-1500:]

    # Did it write to the default login anyway?  Put everything back.
    touched = []
    for i, s in enumerate(sentinels):
        after = _walk(s)
        if after == default_before[i]:
            continue
        for rel in set(after) - set(default_before[i]):
            target = s / rel if rel else s
            if _mentions_leanctx(target):
                touched.append(str(target))
                try:
                    target.unlink()
                except OSError:
                    pass
        for rel, (_mt, size) in default_before[i].items():
            if size > _MAX_BACKUP_BYTES or after.get(rel) == default_before[i][rel]:
                continue
            target = s / rel if rel else s
            src = guard / str(i) / (rel or s.name)
            # Only a change that *introduced* LeanCTX is ours to revert.
            if src.is_file() and _mentions_leanctx(target) and not _mentions_leanctx(src):
                touched.append(str(target))
                shutil.copy2(src, target)
    shutil.rmtree(guard, ignore_errors=True)

    after = _walk(cdir)
    created = sorted(set(after) - set(before))
    modified = sorted(r for r in after if r in before and after[r] != before[r])
    if fresh:
        backup.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(
            {"ts": time.time(), "created": created, "modified": modified}, indent=2))
    else:
        m = json.loads(manifest_path.read_text())
        m["created"] = sorted(set(m.get("created", [])) | set(created))
        manifest_path.write_text(json.dumps(m, indent=2))

    if touched:
        if fresh:
            _disable(profile, quiet=True)
        return {"ok": False, "mode": profile.get("leanctx", "off"), "output": output,
                "message": "lean-ctx ignored the profile's config dir and wrote to the default "
                           f"login ({', '.join(touched)}). Those files were restored; LeanCTX "
                           "was not enabled for this profile."}
    if proc.returncode != 0:
        return {"ok": False, "mode": profile.get("leanctx", "off"), "output": output,
                "message": f"lean-ctx init failed (exit {proc.returncode}): {output[-300:]}"}
    if not created and not modified:
        return {"ok": False, "mode": profile.get("leanctx", "off"), "output": output,
                "message": "lean-ctx init ran but changed nothing inside the profile's config "
                           "dir — it probably does not honour "
                           f"{', '.join(BACKEND_ENV[profile['backend']])}."}

    _set_mode(profile["name"], mode)
    message = f"LeanCTX {mode}: {len(created)} file(s) added, {len(modified)} changed in {cdir}."
    if mode == "shadow":
        message += (" Note: LeanCTX's shadow (measure-only) switch is machine-wide — "
                    "`[shadow] enabled = true` in ~/.config/lean-ctx/config.toml — and is "
                    "left for you to set, since it also affects your default login.")
    return {"ok": True, "mode": mode, "created": created, "modified": modified,
            "output": output, "message": message}


def _disable(profile: dict, quiet: bool = False) -> dict:
    cdir = config_dir_of(profile)
    backup = _backup_dir(profile)
    manifest_path = backup / "manifest.json"
    removed = restored = 0
    if manifest_path.is_file():
        m = json.loads(manifest_path.read_text())
        for rel in m.get("created", []):
            p = cdir / rel
            if p.is_file():
                p.unlink()
                removed += 1
                # Drop directories the init created and that are empty again.
                parent = p.parent
                while parent != cdir and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        for rel in m.get("modified", []):
            src = backup / "profile" / rel
            if src.is_file():
                shutil.copy2(src, cdir / rel)
                restored += 1
        shutil.rmtree(backup, ignore_errors=True)
    if not quiet:
        _set_mode(profile["name"], "off")
    return {"ok": True, "mode": "off",
            "message": (f"LeanCTX removed from {cdir}: {removed} file(s) deleted, "
                        f"{restored} restored.") if (removed or restored)
                       else "LeanCTX is off (nothing to remove)."}
