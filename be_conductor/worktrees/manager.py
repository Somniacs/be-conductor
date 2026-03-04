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

"""Git worktree lifecycle management — create, finalize, merge, remove, GC."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from be_conductor.worktrees import state as wt_state

log = logging.getLogger(__name__)

# Directory name for inside-repo worktrees
_WORKTREE_DIR_NAME = ".be-conductor-worktrees"

# Branch prefix for worktree branches
_BRANCH_PREFIX = "be-conductor/"


def _git(*args: str, cwd: str | Path | None = None, check: bool = True,
         capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + list(args)
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=capture, text=True,
        timeout=30, check=check,
    )


def _git_output(*args: str, cwd: str | Path | None = None) -> str:
    """Run a git command and return stripped stdout."""
    r = _git(*args, cwd=cwd)
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WorktreeInfo:
    """Metadata for a single managed worktree."""
    name: str                   # Session name
    repo_path: str              # Absolute path to the original repo root
    worktree_path: str          # Absolute path to the worktree directory
    branch: str                 # Git branch name (e.g. conductor/my-session)
    base_branch: str            # Branch the worktree was forked from
    base_commit: str            # Commit SHA the worktree was forked from
    session_id: str             # Conductor session ID
    created_at: float           # Unix timestamp
    status: str = "active"      # active | finalized | orphaned | stale
    last_activity: float = 0.0  # Last PTY activity timestamp
    commits_ahead: int = 0      # Number of commits ahead of base
    has_changes: bool = False   # Uncommitted changes in the worktree

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorktreeInfo:
        # Filter to only known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class MergeResult:
    """Result of a merge operation."""
    success: bool
    strategy: str               # squash | merge | rebase
    merged_branch: str
    target_branch: str
    commits_merged: int = 0
    conflict_files: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class MergePreview:
    """Preview of what a merge would do."""
    can_merge: bool
    commits_ahead: int
    commits_behind: int
    conflict_files: list[str] = field(default_factory=list)
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# WorktreeManager
# ---------------------------------------------------------------------------

class WorktreeManager:
    """Manages the lifecycle of git worktrees for Conductor sessions."""

    def __init__(self, active_sessions: set[str] | None = None):
        """Initialize with an optional set of active session IDs for protection."""
        self._active_sessions = active_sessions or set()

    def set_active_sessions(self, session_ids: set[str]):
        """Update the set of active session IDs."""
        self._active_sessions = session_ids

    # -- Validation ----------------------------------------------------------

    @staticmethod
    def find_repo_root(path: str) -> str | None:
        """Find the git repository root for a path. Returns None if not a git repo."""
        try:
            root = _git_output("rev-parse", "--show-toplevel", cwd=path)
            return root
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return None

    @staticmethod
    def check_git_directory(path: str) -> dict[str, Any]:
        """Check if a directory is a git repo and return info for the dashboard.

        Returns dict with:
          is_git: bool
          repo_root: str | None
          current_branch: str | None
          has_remote: bool
          existing_worktrees: int (count of be-conductor-managed worktrees)
          stale_worktrees: int
        """
        result: dict[str, Any] = {
            "is_git": False,
            "repo_root": None,
            "current_branch": None,
            "has_remote": False,
            "existing_worktrees": 0,
            "stale_worktrees": 0,
        }

        try:
            root = _git_output("rev-parse", "--show-toplevel", cwd=path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError, OSError):
            return result

        result["is_git"] = True
        result["repo_root"] = root

        try:
            result["current_branch"] = _git_output(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=root
            )
        except Exception:
            pass

        try:
            remotes = _git_output("remote", cwd=root)
            result["has_remote"] = bool(remotes.strip())
        except Exception:
            pass

        # Count managed worktrees from state
        worktrees = wt_state.get_all_for_repo(root)
        result["existing_worktrees"] = len(worktrees)
        result["stale_worktrees"] = sum(
            1 for w in worktrees.values() if w.get("status") == "stale"
        )

        return result

    # -- Create --------------------------------------------------------------

    def create(self, session_name: str, session_id: str,
               repo_path: str, base_branch: str | None = None) -> WorktreeInfo:
        """Create a new git worktree for a session.

        Args:
            session_name: Human-readable session name
            session_id: Unique session identifier
            repo_path: Path to the git repo root
            base_branch: Branch to fork from (defaults to current HEAD)

        Returns:
            WorktreeInfo with the created worktree metadata

        Raises:
            ValueError: If not a git repo or branch already exists
            subprocess.CalledProcessError: If git commands fail
        """
        root = self.find_repo_root(repo_path)
        if not root:
            raise ValueError(f"Not a git repository: {repo_path}")

        # Determine base branch and commit
        if base_branch is None:
            base_branch = _git_output("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
        base_commit = _git_output("rev-parse", "HEAD", cwd=root)

        # Build branch name: conductor/<session_name>
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '-', session_name).strip('-')
        branch = f"{_BRANCH_PREFIX}{safe_name}"

        # Handle branch collision — append suffix if needed
        try:
            _git("rev-parse", "--verify", branch, cwd=root)
            # Branch exists — try numbered suffixes
            for i in range(2, 100):
                candidate = f"{branch}-{i}"
                try:
                    _git("rev-parse", "--verify", candidate, cwd=root)
                except subprocess.CalledProcessError:
                    branch = candidate
                    break
            else:
                raise ValueError(f"Too many branches with prefix '{branch}'")
        except subprocess.CalledProcessError:
            pass  # Branch doesn't exist, good

        # Worktree path: <repo_root>/.be-conductor-worktrees/<safe_name>
        worktree_dir = Path(root) / _WORKTREE_DIR_NAME
        worktree_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_dir / safe_name

        # Handle path collision
        if worktree_path.exists():
            for i in range(2, 100):
                candidate_path = worktree_dir / f"{safe_name}-{i}"
                if not candidate_path.exists():
                    worktree_path = candidate_path
                    break
            else:
                raise ValueError(f"Too many worktrees with name '{safe_name}'")

        # Create the worktree with a new branch
        _git("worktree", "add", "-b", branch, str(worktree_path), "HEAD", cwd=root)

        # Ensure .be-conductor-worktrees is in .gitignore
        self._ensure_gitignore(root)

        info = WorktreeInfo(
            name=session_name,
            repo_path=root,
            worktree_path=str(worktree_path),
            branch=branch,
            base_branch=base_branch,
            base_commit=base_commit,
            session_id=session_id,
            created_at=time.time(),
            status="active",
            last_activity=time.time(),
        )

        # Persist
        wt_state.update_worktree(root, session_name, info.to_dict())
        log.info("Created worktree %s at %s (branch: %s)", session_name, worktree_path, branch)
        return info

    # -- Finalize (auto-commit on session exit) ------------------------------

    def finalize(self, info: WorktreeInfo) -> WorktreeInfo:
        """Finalize a worktree after its session exits.

        Checks for uncommitted changes and auto-commits them.
        Updates status to 'finalized'.

        Returns updated WorktreeInfo.
        """
        wt_path = info.worktree_path

        if not Path(wt_path).exists():
            log.warning("Worktree path missing during finalize: %s", wt_path)
            info.status = "orphaned"
            wt_state.update_worktree(info.repo_path, info.name, info.to_dict())
            return info

        # Check for uncommitted changes
        try:
            status = _git_output("status", "--porcelain", cwd=wt_path)
        except Exception:
            status = ""

        if status.strip():
            # Auto-commit everything
            try:
                log.info("Auto-committing in %s:\n%s", info.name, status.strip())
                _git("add", "-A", cwd=wt_path)
                _git("commit", "-m",
                     f"be-conductor: auto-commit on session exit ({info.name})",
                     "--allow-empty-message",
                     cwd=wt_path, check=False)
                info.has_changes = False
                log.info("Auto-commit complete for worktree %s", info.name)
            except Exception as e:
                log.warning("Failed to auto-commit in %s: %s", info.name, e)
                info.has_changes = True

        # Count commits ahead of base
        info.commits_ahead = self._count_commits_ahead(info)
        info.status = "finalized"
        info.last_activity = time.time()

        wt_state.update_worktree(info.repo_path, info.name, info.to_dict())
        log.info("Finalized worktree %s (%d commits ahead)",
                 info.name, info.commits_ahead)
        return info

    # -- Status / refresh ----------------------------------------------------

    def get_status(self, info: WorktreeInfo) -> WorktreeInfo:
        """Refresh status of a worktree (check git state)."""
        wt_path = info.worktree_path

        if not Path(wt_path).exists():
            info.status = "orphaned"
            wt_state.update_worktree(info.repo_path, info.name, info.to_dict())
            return info

        # Check for uncommitted changes
        try:
            status = _git_output("status", "--porcelain", cwd=wt_path)
            info.has_changes = bool(status.strip())
        except Exception:
            pass

        # Count commits ahead
        info.commits_ahead = self._count_commits_ahead(info)

        wt_state.update_worktree(info.repo_path, info.name, info.to_dict())
        return info

    def update_activity(self, info: WorktreeInfo) -> None:
        """Update last activity timestamp (called from session on PTY output)."""
        info.last_activity = time.time()
        wt_state.update_worktree(info.repo_path, info.name, info.to_dict())

    # -- List ----------------------------------------------------------------

    def list_worktrees(self, repo_path: str | None = None) -> list[WorktreeInfo]:
        """List all managed worktrees, optionally filtered by repo."""
        all_data = wt_state.get_all()
        result = []

        if repo_path:
            root = self.find_repo_root(repo_path)
            repos = {root: all_data.get(root, {})} if root and root in all_data else {}
        else:
            repos = all_data

        for _repo, worktrees in repos.items():
            for _name, info_dict in worktrees.items():
                try:
                    result.append(WorktreeInfo.from_dict(info_dict))
                except Exception:
                    pass

        return result

    # -- Remove --------------------------------------------------------------

    def remove(self, info: WorktreeInfo, force: bool = False) -> bool:
        """Remove a worktree and its branch.

        Args:
            info: WorktreeInfo to remove
            force: If True, remove even if there are uncommitted changes

        Returns:
            True if successfully removed

        Raises:
            ValueError: If session is still active and force is False
        """
        if info.session_id in self._active_sessions and not force:
            raise ValueError(
                f"Cannot remove worktree for active session '{info.name}'. "
                "Stop the session first."
            )

        wt_path = Path(info.worktree_path)

        # Remove git worktree
        if wt_path.exists():
            try:
                _git("worktree", "remove", str(wt_path), "--force",
                     cwd=info.repo_path)
            except subprocess.CalledProcessError:
                # Fallback: force remove
                try:
                    _git("worktree", "remove", str(wt_path), "--force",
                         cwd=info.repo_path, check=False)
                    # If git worktree remove fails completely, try manual cleanup
                    import shutil
                    if wt_path.exists():
                        shutil.rmtree(wt_path, ignore_errors=True)
                    _git("worktree", "prune", cwd=info.repo_path, check=False)
                except Exception as e:
                    log.warning("Failed to remove worktree directory %s: %s", wt_path, e)

        # Delete the branch — try safe delete first to preserve unmerged work
        r = _git("branch", "-d", info.branch, cwd=info.repo_path, check=False)
        if r.returncode != 0:
            if force:
                _git("branch", "-D", info.branch, cwd=info.repo_path, check=False)
            else:
                log.info("Keeping branch %s (unmerged commits still recoverable)",
                         info.branch)

        # Remove from state
        wt_state.remove_worktree(info.repo_path, info.name)
        log.info("Removed worktree %s", info.name)
        return True

    # -- Merge ---------------------------------------------------------------

    def preview_merge(self, info: WorktreeInfo) -> MergePreview:
        """Preview what merging a worktree branch would do."""
        repo = info.repo_path
        branch = info.branch
        target = info.base_branch

        # Fetch latest state
        try:
            _git("fetch", "origin", target, cwd=repo, check=False)
        except Exception:
            pass

        # Commits ahead/behind
        ahead = self._count_commits_ahead(info)
        behind = 0
        try:
            behind_str = _git_output(
                "rev-list", "--count", f"{branch}..{target}", cwd=repo
            )
            behind = int(behind_str)
        except Exception:
            pass

        # Changed files
        changed_files = []
        try:
            diff = _git_output(
                "diff", "--stat", "--name-status",
                f"{info.base_commit}...{branch}", cwd=repo
            )
            for line in diff.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    changed_files.append({
                        "status": parts[0].strip(),
                        "path": parts[1].strip(),
                    })
        except Exception:
            pass

        # Check for conflicts using git merge-tree (git 2.38+)
        conflict_files = []
        can_merge = True
        try:
            r = _git("merge-tree", "--write-tree", "--no-messages",
                      target, branch, cwd=repo, check=False)
            if r.returncode != 0:
                can_merge = False
                # Parse conflict files from stderr
                for line in (r.stderr or "").split("\n"):
                    line = line.strip()
                    if line and not line.startswith("CONFLICT"):
                        conflict_files.append(line)
                    elif "CONFLICT" in line:
                        # Extract filename from conflict message
                        match = re.search(r'CONFLICT.*?:\s+(.+)', line)
                        if match:
                            conflict_files.append(match.group(1))
        except Exception:
            # git merge-tree --write-tree not available (git < 2.38)
            # Fall back to a dry-run merge approach
            pass

        message = ""
        if not can_merge:
            message = f"{len(conflict_files)} conflict(s) detected"
        elif ahead == 0:
            message = "Nothing to merge"
            can_merge = False

        return MergePreview(
            can_merge=can_merge,
            commits_ahead=ahead,
            commits_behind=behind,
            conflict_files=conflict_files,
            changed_files=changed_files,
            message=message,
        )

    def merge(self, info: WorktreeInfo, strategy: str = "squash",
              message: str | None = None) -> MergeResult:
        """Merge a worktree branch back into its base branch.

        Uses a temporary worktree for the merge to avoid touching the user's
        main checkout (which may have uncommitted work or another agent).

        Args:
            info: WorktreeInfo to merge
            strategy: 'squash' (default), 'merge', or 'rebase'
            message: Custom commit message (auto-generated if None)

        Returns:
            MergeResult with success/failure info
        """
        # Block merge while session is still running
        if info.session_id in self._active_sessions:
            return MergeResult(
                success=False,
                strategy=strategy,
                merged_branch=info.branch,
                target_branch=info.base_branch,
                message=f"Cannot merge: session '{info.name}' is still running. Stop it first.",
            )

        repo = info.repo_path
        branch = info.branch
        target = info.base_branch
        wt_path = info.worktree_path

        # Auto-commit any uncommitted changes before merging
        if Path(wt_path).exists():
            try:
                status = _git_output("status", "--porcelain", cwd=wt_path)
                if status.strip():
                    log.info("Auto-committing before merge in %s", info.name)
                    _git("add", "-A", cwd=wt_path)
                    _git("commit", "-m",
                         f"be-conductor: auto-commit before merge ({info.name})",
                         "--allow-empty-message",
                         cwd=wt_path, check=False)
            except Exception as e:
                log.warning("Failed to auto-commit before merge in %s: %s", info.name, e)

        # Count commits to merge
        commits_ahead = self._count_commits_ahead(info)
        if commits_ahead == 0:
            return MergeResult(
                success=False,
                strategy=strategy,
                merged_branch=branch,
                target_branch=target,
                message="Nothing to merge (0 commits ahead)",
            )

        if message is None:
            message = f"Merge be-conductor session '{info.name}' ({commits_ahead} commits)"

        # Create a temporary worktree for the merge operation
        tmp_wt_dir = Path(repo) / _WORKTREE_DIR_NAME / f".merge-tmp-{info.name}"
        tmp_branch = f"be-conductor/merge-tmp-{int(time.time())}"

        try:
            # Create temp worktree on the target branch
            _git("worktree", "add", "-b", tmp_branch, str(tmp_wt_dir), target,
                 cwd=repo)

            try:
                if strategy == "squash":
                    r = _git("merge", "--squash", branch, cwd=str(tmp_wt_dir),
                             check=False)
                    if r.returncode != 0:
                        conflict_files = self._parse_conflict_files(str(tmp_wt_dir))
                        return MergeResult(
                            success=False, strategy=strategy,
                            merged_branch=branch, target_branch=target,
                            conflict_files=conflict_files,
                            message="Merge conflicts detected",
                        )
                    _git("commit", "-m", message, cwd=str(tmp_wt_dir))

                elif strategy == "merge":
                    r = _git("merge", "--no-ff", "-m", message, branch,
                             cwd=str(tmp_wt_dir), check=False)
                    if r.returncode != 0:
                        conflict_files = self._parse_conflict_files(str(tmp_wt_dir))
                        return MergeResult(
                            success=False, strategy=strategy,
                            merged_branch=branch, target_branch=target,
                            conflict_files=conflict_files,
                            message="Merge conflicts detected",
                        )

                elif strategy == "rebase":
                    # Rebase the session branch onto the target, then fast-forward
                    r = _git("rebase", target, branch, cwd=str(tmp_wt_dir),
                             check=False)
                    if r.returncode != 0:
                        _git("rebase", "--abort", cwd=str(tmp_wt_dir), check=False)
                        return MergeResult(
                            success=False, strategy=strategy,
                            merged_branch=branch, target_branch=target,
                            message="Rebase conflicts detected",
                        )
                    _git("checkout", target, cwd=str(tmp_wt_dir))
                    _git("merge", "--ff-only", branch, cwd=str(tmp_wt_dir))

                else:
                    return MergeResult(
                        success=False, strategy=strategy,
                        merged_branch=branch, target_branch=target,
                        message=f"Unknown strategy: {strategy}",
                    )

                # Check if target branch is currently checked out in main repo
                sync_worktree = False
                try:
                    current = _git_output(
                        "rev-parse", "--abbrev-ref", "HEAD", cwd=repo
                    )
                    sync_worktree = current == target
                except Exception:
                    pass

                # Stash any uncommitted work before we touch the working tree
                stashed = False
                if sync_worktree:
                    r = _git("stash", "push", "-m",
                             "conductor-merge-autostash",
                             cwd=repo, check=False)
                    stashed = "No local changes" not in (r.stdout or "")

                # Update the target branch ref in the main repo
                merge_commit = _git_output("rev-parse", "HEAD", cwd=str(tmp_wt_dir))
                _git("update-ref", f"refs/heads/{target}", merge_commit, cwd=repo)

                # Sync working tree to match the new ref, then restore stash
                if sync_worktree:
                    _git("reset", "--hard", "HEAD", cwd=repo, check=False)
                    if stashed:
                        _git("stash", "pop", cwd=repo, check=False)

            finally:
                # Clean up temp worktree
                _git("worktree", "remove", str(tmp_wt_dir), "--force",
                     cwd=repo, check=False)
                _git("branch", "-D", tmp_branch, cwd=repo, check=False)

        except subprocess.CalledProcessError as e:
            return MergeResult(
                success=False, strategy=strategy,
                merged_branch=branch, target_branch=target,
                message=f"Git error: {e.stderr or e.stdout or str(e)}",
            )

        # Success — advance base_commit to worktree branch HEAD so
        # _count_commits_ahead returns 0 (squash creates a different commit
        # on target, so comparing against target HEAD would still show ahead)
        try:
            new_base = _git_output("rev-parse", branch, cwd=repo)
            info.base_commit = new_base
            info.commits_ahead = 0
            wt_state.update_worktree(repo, info.name, info.to_dict())
        except Exception as e:
            log.warning("Failed to update base_commit after merge: %s", e)

        log.info("Merged worktree %s into %s (strategy: %s, %d commits)",
                 info.name, target, strategy, commits_ahead)

        return MergeResult(
            success=True,
            strategy=strategy,
            merged_branch=branch,
            target_branch=target,
            commits_merged=commits_ahead,
            message=f"Successfully merged {commits_ahead} commit(s) into {target}",
        )

    # -- Diff ----------------------------------------------------------------

    def get_diff(self, info: WorktreeInfo, files_only: bool = False) -> str | list[dict]:
        """Get the diff for a worktree branch vs its base.

        For finalized worktrees, compares committed branch state vs base.
        For active worktrees, also includes uncommitted and untracked changes.

        Args:
            info: WorktreeInfo
            files_only: If True, return list of {path, status, additions, deletions}

        Returns:
            Full diff string, or list of file dicts if files_only=True
        """
        repo = info.repo_path
        base = info.base_commit
        branch = info.branch

        # Active worktrees: diff working tree (including uncommitted changes)
        # against the base commit, run from the worktree directory.
        active = (info.status == "active"
                  and info.worktree_path
                  and Path(info.worktree_path).is_dir())

        if files_only:
            try:
                if active:
                    output = _git_output(
                        "diff", "--numstat", base, cwd=info.worktree_path
                    )
                else:
                    output = _git_output(
                        "diff", "--numstat", f"{base}...{branch}", cwd=repo
                    )
                files = []
                for line in output.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t")
                    if len(parts) == 3:
                        adds = int(parts[0]) if parts[0] != "-" else 0
                        dels = int(parts[1]) if parts[1] != "-" else 0
                        files.append({
                            "path": parts[2],
                            "additions": adds,
                            "deletions": dels,
                        })
                # For active worktrees, also include untracked files
                if active:
                    untracked = _git_output(
                        "ls-files", "--others", "--exclude-standard",
                        cwd=info.worktree_path
                    )
                    for f in untracked.split("\n"):
                        f = f.strip()
                        if not f:
                            continue
                        fpath = Path(info.worktree_path) / f
                        lines = fpath.read_text(errors="replace").count("\n")
                        files.append({
                            "path": f,
                            "additions": lines,
                            "deletions": 0,
                        })
                return files
            except Exception:
                return []
        else:
            try:
                if active:
                    # Diff tracked files (committed + uncommitted) vs base
                    diff = _git_output("diff", base, cwd=info.worktree_path)
                    # Append untracked (new) files as diff hunks
                    untracked = _git_output(
                        "ls-files", "--others", "--exclude-standard",
                        cwd=info.worktree_path
                    )
                    for f in untracked.split("\n"):
                        f = f.strip()
                        if not f:
                            continue
                        fpath = Path(info.worktree_path) / f
                        try:
                            content = fpath.read_text(errors="replace")
                        except Exception:
                            continue
                        lines = content.split("\n")
                        hdr = (f"diff --git a/{f} b/{f}\n"
                               f"new file mode 100644\n"
                               f"--- /dev/null\n"
                               f"+++ b/{f}\n"
                               f"@@ -0,0 +1,{len(lines)} @@\n")
                        hdr += "\n".join(f"+{l}" for l in lines)
                        if diff:
                            diff += "\n" + hdr
                        else:
                            diff = hdr
                    return diff
                else:
                    return _git_output("diff", f"{base}...{branch}", cwd=repo)
            except Exception:
                return ""

    # -- Rich diff (per-file base/head content) --------------------------------

    def get_rich_diff(self, info: WorktreeInfo) -> list[dict]:
        """Get per-file base/head content for a worktree branch vs its base.

        Returns a list of dicts, each with:
            path:         relative file path
            status:       "added" | "modified" | "deleted" | "renamed"
            base_content: content at base commit (empty for added files)
            head_content: content at head/worktree (empty for deleted files)
        """
        repo = info.repo_path
        base = info.base_commit
        branch = info.branch
        active = (info.status == "active"
                  and info.worktree_path
                  and Path(info.worktree_path).is_dir())

        result: list[dict] = []
        status_map = {"M": "modified", "A": "added", "D": "deleted",
                      "R": "renamed", "C": "copied", "T": "modified"}

        try:
            # Detect binary files so we can skip them
            if active:
                numstat = _git_output("diff", "--numstat", base,
                                      cwd=info.worktree_path)
            else:
                numstat = _git_output("diff", "--numstat",
                                      f"{base}...{branch}", cwd=repo)
            binary_paths: set[str] = set()
            for line in numstat.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) == 3 and parts[0] == "-":
                    binary_paths.add(parts[2])

            # Get list of changed files with status
            if active:
                raw = _git_output("diff", "--name-status", base,
                                  cwd=info.worktree_path)
            else:
                raw = _git_output("diff", "--name-status",
                                  f"{base}...{branch}", cwd=repo)

            for line in raw.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                status_code = parts[0][0]
                file_path = parts[-1]
                status = status_map.get(status_code, "modified")

                if file_path in binary_paths:
                    continue

                # Base content
                base_content = ""
                if status not in ("added", "copied"):
                    old_path = parts[1] if status_code.startswith("R") else file_path
                    try:
                        base_content = _git_output("show",
                                                   f"{base}:{old_path}",
                                                   cwd=repo)
                    except Exception:
                        base_content = ""

                # Head content
                head_content = ""
                if status != "deleted":
                    try:
                        if active:
                            fpath = Path(info.worktree_path) / file_path
                            head_content = fpath.read_text(errors="replace")
                        else:
                            head_content = _git_output("show",
                                                       f"{branch}:{file_path}",
                                                       cwd=repo)
                    except Exception:
                        head_content = ""

                result.append({
                    "path": file_path,
                    "status": status,
                    "base_content": base_content,
                    "head_content": head_content,
                })

            # For active worktrees, include untracked (new) files
            if active:
                untracked = _git_output("ls-files", "--others",
                                        "--exclude-standard",
                                        cwd=info.worktree_path)
                for f in untracked.split("\n"):
                    f = f.strip()
                    if not f:
                        continue
                    fpath = Path(info.worktree_path) / f
                    try:
                        content = fpath.read_text(errors="replace")
                    except Exception:
                        continue
                    result.append({
                        "path": f,
                        "status": "added",
                        "base_content": "",
                        "head_content": content,
                    })

        except Exception:
            log.exception("get_rich_diff failed for worktree %s", info.name)

        return result

    # -- Reconcile (crash recovery) ------------------------------------------

    def reconcile(self) -> dict[str, list[str]]:
        """Cross-reference persisted state with actual git worktrees.

        Called on server start. Detects orphaned worktrees (state exists but
        directory missing) and untracked worktrees (directory exists but no
        state entry).

        Returns dict with 'orphaned', 'recovered', 'cleaned' lists.
        """
        result: dict[str, list[str]] = {
            "orphaned": [],
            "recovered": [],
            "cleaned": [],
        }

        all_data = wt_state.get_all()
        for repo_path, worktrees in list(all_data.items()):
            for name, info_dict in list(worktrees.items()):
                wt_path = info_dict.get("worktree_path", "")

                if not Path(wt_path).exists():
                    # State exists but directory is gone
                    info_dict["status"] = "orphaned"
                    wt_state.update_worktree(repo_path, name, info_dict)
                    result["orphaned"].append(name)
                    log.warning("Worktree %s marked as orphaned (path missing: %s)",
                                name, wt_path)
                elif info_dict.get("status") == "active":
                    # Was active when server crashed — check if session is still running
                    session_id = info_dict.get("session_id", "")
                    if session_id not in self._active_sessions:
                        # Session is gone — finalize the worktree
                        try:
                            info = WorktreeInfo.from_dict(info_dict)
                            self.finalize(info)
                            result["recovered"].append(name)
                            log.info("Recovered orphaned worktree %s (session gone)", name)
                        except Exception as e:
                            log.warning("Failed to recover worktree %s: %s", name, e)

        return result

    # -- GC ------------------------------------------------------------------

    def gc(self, max_age_days: float = 7.0, dry_run: bool = False) -> list[dict]:
        """Garbage-collect stale and orphaned worktrees.

        Args:
            max_age_days: Remove finalized/orphaned worktrees older than this
            dry_run: If True, report what would be removed without doing it

        Returns:
            List of {name, repo, status, reason, action} dicts
        """
        cutoff = time.time() - (max_age_days * 86400)
        actions = []

        all_data = wt_state.get_all()
        for repo_path, worktrees in list(all_data.items()):
            for name, info_dict in list(worktrees.items()):
                status = info_dict.get("status", "active")
                session_id = info_dict.get("session_id", "")
                last_activity = info_dict.get("last_activity", 0)

                # Never GC active sessions
                if session_id in self._active_sessions:
                    continue

                reason = None
                if status == "orphaned":
                    reason = "orphaned (path missing)"
                elif status in ("finalized", "stale") and last_activity < cutoff:
                    reason = f"stale ({status}, inactive > {max_age_days}d)"

                if reason:
                    action = {
                        "name": name,
                        "repo": repo_path,
                        "status": status,
                        "reason": reason,
                        "action": "would remove" if dry_run else "removed",
                    }
                    actions.append(action)

                    if not dry_run:
                        try:
                            info = WorktreeInfo.from_dict(info_dict)
                            self.remove(info, force=True)
                        except Exception as e:
                            action["action"] = f"failed: {e}"
                            log.warning("GC failed for %s: %s", name, e)

        return actions

    # -- Warnings / health ---------------------------------------------------

    def get_warnings(self) -> list[dict[str, Any]]:
        """Get health warnings for worktrees (stale, orphaned, etc.)."""
        warnings = []
        stale_threshold = time.time() - (3 * 86400)  # 3 days

        for info in self.list_worktrees():
            if info.status == "orphaned":
                warnings.append({
                    "name": info.name,
                    "repo": info.repo_path,
                    "level": "error",
                    "message": f"Worktree '{info.name}' is orphaned (directory missing)",
                })
            elif (info.status == "finalized"
                  and info.last_activity < stale_threshold):
                age_days = (time.time() - info.last_activity) / 86400
                warnings.append({
                    "name": info.name,
                    "repo": info.repo_path,
                    "level": "warning",
                    "message": (f"Worktree '{info.name}' has been idle for "
                                f"{age_days:.0f} days. Consider merging or discarding."),
                })
            elif (info.status == "active"
                  and info.session_id not in self._active_sessions
                  and info.last_activity < stale_threshold):
                warnings.append({
                    "name": info.name,
                    "repo": info.repo_path,
                    "level": "warning",
                    "message": f"Worktree '{info.name}' has no active session and is idle.",
                })

        return warnings

    # -- Private helpers -----------------------------------------------------

    def _count_commits_ahead(self, info: WorktreeInfo) -> int:
        """Count commits on the worktree branch ahead of the base commit."""
        try:
            count_str = _git_output(
                "rev-list", "--count",
                f"{info.base_commit}..{info.branch}",
                cwd=info.repo_path,
            )
            return int(count_str)
        except Exception:
            return 0

    @staticmethod
    def _parse_conflict_files(worktree_path: str) -> list[str]:
        """Parse conflicted files from a failed merge in a worktree."""
        try:
            output = _git_output(
                "diff", "--name-only", "--diff-filter=U", cwd=worktree_path
            )
            return [f for f in output.strip().split("\n") if f.strip()]
        except Exception:
            return []

    @staticmethod
    def _ensure_gitignore(repo_root: str) -> None:
        """Ensure .be-conductor-worktrees/ is excluded from git.

        Uses .git/info/exclude (local, never committed) instead of the
        repo's .gitignore to avoid polluting the tracked working tree.
        """
        exclude = Path(repo_root) / ".git" / "info" / "exclude"
        entry = f"/{_WORKTREE_DIR_NAME}/"

        if exclude.exists():
            content = exclude.read_text()
            if entry in content or _WORKTREE_DIR_NAME in content:
                return
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n# Conductor worktrees\n{entry}\n"
            exclude.write_text(content)
        else:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(f"# Conductor worktrees\n{entry}\n")
