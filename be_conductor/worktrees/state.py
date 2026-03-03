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

"""Persistent state for worktree metadata, stored in ~/.be-conductor/worktrees.json."""

import json
import tempfile
from pathlib import Path
from typing import Any

from be_conductor.utils.config import WORKTREES_FILE


def load() -> dict[str, Any]:
    """Load the full worktree state from disk. Returns {repo_path: {name: info}}."""
    if not WORKTREES_FILE.exists():
        return {}
    try:
        return json.loads(WORKTREES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save(data: dict[str, Any]) -> None:
    """Atomically write the full worktree state to disk."""
    WORKTREES_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file then rename
    fd, tmp = tempfile.mkstemp(
        dir=str(WORKTREES_FILE.parent), suffix=".tmp"
    )
    try:
        with open(fd, "w") as f:
            json.dump(data, f, indent=2)
        Path(tmp).replace(WORKTREES_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def get_worktree(repo_path: str, name: str) -> dict[str, Any] | None:
    """Get a single worktree entry by repo path and session name."""
    data = load()
    return data.get(repo_path, {}).get(name)


def update_worktree(repo_path: str, name: str, info: dict[str, Any]) -> None:
    """Create or update a worktree entry."""
    data = load()
    if repo_path not in data:
        data[repo_path] = {}
    data[repo_path][name] = info
    save(data)


def remove_worktree(repo_path: str, name: str) -> None:
    """Remove a worktree entry."""
    data = load()
    repo = data.get(repo_path, {})
    repo.pop(name, None)
    if not repo:
        data.pop(repo_path, None)
    save(data)


def get_all_for_repo(repo_path: str) -> dict[str, Any]:
    """Get all worktree entries for a given repo."""
    data = load()
    return data.get(repo_path, {})


def get_all() -> dict[str, dict[str, Any]]:
    """Get the full state — all repos and their worktrees."""
    return load()
