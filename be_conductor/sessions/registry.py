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

"""In-memory session registry with metadata persisted to disk."""

import asyncio
import json
import logging
import re
import shlex
from typing import Dict, Optional

from be_conductor.notifications.manager import (
    NotificationManager, SessionNotifier, _DEFAULT_PATTERNS,
)
from be_conductor.sessions.session import Session, _ANSI_RE
from be_conductor.utils import config as cfg
from be_conductor.utils.config import SESSIONS_DIR, ensure_dirs

log = logging.getLogger(__name__)


class SessionRegistry:
    """In-memory registry of all sessions, with metadata persisted to disk.

    Running sessions live in ``self.sessions``.  When a session exits and
    its terminal output matches a configured resume pattern (e.g. Claude
    Code's ``--resume <id>``), its metadata is kept in ``self.resumable``
    so the user can resume the conversation later — even after a server
    restart.  Resume patterns are configured per command in
    ``ALLOWED_COMMANDS`` (see config.py).
    """

    def __init__(self):
        self.sessions: Dict[str, Session] = {}
        self.resumable: Dict[str, dict] = {}
        self._worktree_manager = None  # Lazy-initialized
        self.notification_manager = NotificationManager()
        ensure_dirs()
        self._load_resumable()

    @property
    def worktree_manager(self):
        """Lazy-initialize the WorktreeManager with current active session IDs."""
        if self._worktree_manager is None:
            from be_conductor.worktrees.manager import WorktreeManager
            self._worktree_manager = WorktreeManager(
                active_sessions=set(self.sessions.keys())
            )
        else:
            self._worktree_manager.set_active_sessions(set(self.sessions.keys()))
        return self._worktree_manager

    @staticmethod
    def _agent_config_for(command: str) -> dict:
        """Return per-command config fields for a command.

        Matches the command's base executable against ALLOWED_COMMANDS entries
        and returns resume_pattern, resume_flag, and stop_sequence (if any).
        """
        try:
            base = shlex.split(command)[0]
        except ValueError:
            return {}
        for entry in cfg.ALLOWED_COMMANDS:
            try:
                entry_base = shlex.split(entry["command"])[0]
            except ValueError:
                continue
            if base == entry_base:
                return {
                    k: entry[k]
                    for k in ("resume_pattern", "resume_flag", "resume_command",
                              "stop_sequence", "notification_patterns")
                    if k in entry
                }
        return {}

    def _load_resumable(self):
        """Load persisted resumable-session metadata from disk on startup.

        Sessions with status ``exited`` and a resume token or worktree are
        loaded normally.  Sessions still marked ``running`` or ``stopping``
        represent a previous unclean shutdown — the process is long gone.
        If the command contains a ``--resume <id>`` flag from a previous
        resume, we recover the token so the session can be resumed again.
        """
        import re as _re
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                meta = json.loads(path.read_text())
                status = meta.get("status", "")

                # Recover sessions left in running/stopping state after a crash
                if status in ("running", "starting", "stopping"):
                    cmd = meta.get("command", "")
                    flag = meta.get("resume_flag", "--resume")
                    m = _re.search(rf'{_re.escape(flag)}\s+(\S+)', cmd)
                    if m:
                        meta["resume_id"] = m.group(1)
                    meta["status"] = "exited"
                    if meta.get("resume_id") or meta.get("worktree"):
                        self.resumable[meta["id"]] = meta
                        path.write_text(json.dumps(meta))
                    else:
                        path.unlink(missing_ok=True)
                elif status == "exited" and (
                    meta.get("resume_id") or meta.get("worktree")
                ):
                    self.resumable[meta["id"]] = meta
                elif status == "exited":
                    path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _on_session_exit(self, session_id: str):
        """Called when a session's process exits."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        # "Forget" mode: delete everything without saving resume data.
        if getattr(session, '_forget', False):
            self._delete_metadata(session_id)
            return

        # Worktree stays active on exit — user must explicitly finalize.
        # No auto-commit here; the worktree is just a working directory.

        # Keep session as resumable when it has a resume token, a worktree,
        # or was explicitly stopped via graceful stop ("Stop & keep for later").
        was_graceful = session.status == "stopping"
        if session.resume_id or session.worktree or was_graceful:
            meta = session.to_dict()
            meta["status"] = "exited"
            self.resumable[session_id] = meta
            self._save_metadata_dict(meta)
        else:
            self._delete_metadata(session_id)

    async def create(self, name: str, command: str, cwd: str | None = None,
                     env: dict | None = None, rows: int | None = None,
                     cols: int | None = None, source: str | None = None,
                     worktree: bool = False) -> Session:
        if name in self.sessions:
            existing = self.sessions[name]
            if existing.status == "running":
                raise ValueError(f"Session '{name}' already exists and is running")
            else:
                await self.remove(name)

        # If resuming over an old resumable entry with the same name, clear it.
        self.resumable.pop(name, None)

        agent_cfg = self._agent_config_for(command)

        # Create worktree if requested
        worktree_info = None
        session_cwd = cwd
        if worktree and cwd:
            try:
                wt_info = self.worktree_manager.create(
                    session_name=name,
                    session_id=name,
                    repo_path=cwd,
                )
                worktree_info = wt_info.to_dict()
                session_cwd = wt_info.worktree_path
                log.info("Created worktree for session '%s' at %s", name, session_cwd)
            except Exception as e:
                raise ValueError(f"Failed to create worktree: {e}")

        # Build notification patterns for this agent
        custom_patterns = agent_cfg.get("notification_patterns")
        if custom_patterns and isinstance(custom_patterns, list):
            notif_patterns = [re.compile(p, re.IGNORECASE) for p in custom_patterns]
        else:
            notif_patterns = None  # use defaults

        notifier = SessionNotifier(
            session_id=name,
            session_name=name,
            manager=self.notification_manager,
            patterns=notif_patterns,
        )

        session = Session(
            name=name,
            command=command,
            session_id=name,
            cwd=session_cwd,
            on_exit=self._on_session_exit,
            env=env,
            resume_pattern=agent_cfg.get("resume_pattern"),
            resume_flag=agent_cfg.get("resume_flag"),
            resume_command=agent_cfg.get("resume_command"),
            stop_sequence=agent_cfg.get("stop_sequence"),
            worktree=worktree_info,
            notifier=notifier,
        )
        start_rows, start_cols = rows or 24, cols or 80
        await session.start(rows=start_rows, cols=start_cols)
        # Record initial size so the web client knows the PTY dimensions.
        if rows and cols and source == "cli":
            session.resize(rows, cols, source="cli")
        self.sessions[name] = session
        self._save_metadata(session)
        return session

    async def resume(self, session_id: str, rows: int | None = None,
                     cols: int | None = None) -> Session:
        """Resume a previously exited session using its stored resume ID.

        Two modes:

        1. **Token-based** (Claude Code, Copilot) — a ``resume_pattern``
           captures a token from the terminal output and ``resume_flag``
           appends it to the original command, e.g.
           ``claude ... --resume <id>``.
        2. **Command-based** (Codex) — a fixed ``resume_command`` replaces
           the original command entirely, e.g. ``codex resume --last``.
        """
        meta = self.resumable.pop(session_id, None)

        # Edge case: session just exited but _on_session_exit hasn't moved
        # it to self.resumable yet — check self.sessions as a fallback.
        if not meta:
            live = self.sessions.get(session_id)
            if live and live.status == "exited" and live.resume_id:
                meta = live.to_dict()
                self.sessions.pop(session_id, None)
                if live._monitor_task:
                    live._monitor_task.cancel()
                    try:
                        await live._monitor_task
                    except asyncio.CancelledError:
                        pass
                live.pty.close()

        if not meta:
            raise ValueError(f"No resumable session '{session_id}'")

        has_resume_id = bool(meta.get("resume_id"))
        has_worktree = bool(meta.get("worktree"))

        if not has_resume_id and not has_worktree:
            raise ValueError(f"No resumable session '{session_id}'")

        if has_resume_id:
            # Command-based resume (e.g. "codex resume --last") — use as-is.
            if meta.get("resume_command"):
                command = meta["resume_command"]
            else:
                # Token-based resume — append flag + captured ID to original command.
                flag = meta.get("resume_flag", "--resume")
                # Strip any previous resume flag+id from the command to avoid
                # accumulation across multiple resumes.
                import re as _re
                command = _re.sub(
                    rf'\s*{_re.escape(flag)}\s+\S+', '', meta["command"]
                ).rstrip()
                command += f" {flag} {meta['resume_id']}"
        else:
            # Worktree without resume token — restart original command in the worktree
            command = meta["command"]

        cwd = meta.get("cwd")
        worktree_data = meta.get("worktree")

        # If the session had a worktree, verify it still exists and resume there
        if worktree_data:
            from pathlib import Path
            wt_path = worktree_data.get("worktree_path", "")
            if wt_path and Path(wt_path).exists():
                cwd = wt_path
                # Mark worktree as active again
                worktree_data["status"] = "active"
            else:
                log.warning("Worktree path missing for resume: %s", wt_path)
                worktree_data = None

        self._delete_metadata(session_id)

        # Create the resumed session (don't create a new worktree — reuse existing)
        session = await self.create(meta["name"], command, cwd=cwd,
                                    rows=rows, cols=cols)

        # Re-attach the worktree info
        if worktree_data:
            session.worktree = worktree_data
            self._save_metadata(session)
            from be_conductor.worktrees import state as wt_state
            wt_state.update_worktree(
                worktree_data["repo_path"], meta["name"], worktree_data
            )

        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def list_all(self) -> list[dict]:
        live = [s.to_dict() for s in self.sessions.values()]
        # Refresh worktree commits_ahead for resumable sessions
        resumable = []
        for meta in self.resumable.values():
            if meta.get("worktree") and self.worktree_manager:
                try:
                    from be_conductor.worktrees.manager import WorktreeInfo
                    info = WorktreeInfo.from_dict(meta["worktree"])
                    ahead = self.worktree_manager._count_commits_ahead(info)
                    if ahead != meta["worktree"].get("commits_ahead", 0):
                        meta["worktree"]["commits_ahead"] = ahead
                except Exception:
                    pass
            resumable.append(meta)
        return live + resumable

    async def remove(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            await session.kill()
            await session.cleanup()
            # Try to extract resume info even after hard kill
            session._extract_resume_id()
            if session.resume_id or session.worktree:
                meta = session.to_dict()
                meta["status"] = "exited"
                self.resumable[session_id] = meta
                self._save_metadata_dict(meta)
            else:
                self._delete_metadata(session_id)

    def graceful_stop(self, session_id: str):
        """Send SIGINT to the session for a graceful shutdown.

        The session stays in ``self.sessions`` — its ``_monitor_process``
        task will detect the exit, extract any resume token from the
        terminal buffer, and call ``_on_session_exit`` which moves the
        session to ``self.resumable`` if a resume ID was found.
        """
        session = self.sessions.get(session_id)
        if session and session.status in ("running", "starting"):
            session.interrupt(timeout=cfg.GRACEFUL_STOP_TIMEOUT)

    def forget(self, session_id: str):
        """Gracefully stop a session and discard it without saving resume data.

        Like ``graceful_stop``, this sends SIGINT so the agent can clean up,
        but marks the session so that ``_on_session_exit`` deletes all
        metadata instead of saving it as resumable.
        """
        session = self.sessions.get(session_id)
        if session and session.status in ("running", "starting"):
            session._forget = True
            session.interrupt(timeout=cfg.GRACEFUL_STOP_TIMEOUT)

    def dismiss_resumable(self, session_id: str):
        """Remove a resumable entry without resuming it."""
        self.resumable.pop(session_id, None)
        self._delete_metadata(session_id)

    def clear_all_resumable(self) -> int:
        """Remove all resumable entries that have no worktree. Returns count removed."""
        to_remove = [
            sid for sid, meta in self.resumable.items()
            if not meta.get("worktree")
        ]
        for sid in to_remove:
            self.resumable.pop(sid, None)
            self._delete_metadata(sid)
        return len(to_remove)

    def _save_metadata(self, session: Session):
        path = SESSIONS_DIR / f"{session.id}.json"
        path.write_text(json.dumps(session.to_dict(), indent=2))

    def _save_metadata_dict(self, meta: dict):
        path = SESSIONS_DIR / f"{meta['id']}.json"
        path.write_text(json.dumps(meta, indent=2))

    def _delete_metadata(self, session_id: str):
        path = SESSIONS_DIR / f"{session_id}.json"
        path.unlink(missing_ok=True)

    async def cleanup_all(self):
        """Gracefully stop all sessions, preserving resume tokens.

        Phase 0: Send ESC to every running session to break agents out of
        menus, selection prompts, or mid-thought states.
        Phase 1: Interrupt every running session (sends SIGINT / stop
        sequence so agents can print resume tokens).
        Phase 2: Wait up to 10 s for processes to exit — ``_monitor_process``
        extracts the resume token and ``_on_session_exit`` persists it.
        Phase 3: Hard-kill any stragglers.
        """
        ids = list(self.sessions.keys())
        if not ids:
            return

        # Phase 0 — send ESC to break out of menus / thinking states
        for sid in ids:
            session = self.sessions.get(sid)
            if session and session.status in ("running", "starting"):
                try:
                    session.pty.write(b"\x1b")
                except OSError:
                    pass
        await asyncio.sleep(0.3)

        # Phase 1 — graceful interrupt
        for sid in ids:
            session = self.sessions.get(sid)
            if session and session.status in ("running", "starting"):
                session.interrupt(timeout=30)

        # Phase 2 — wait for exits (resume tokens are extracted here)
        for _ in range(50):                       # 50 × 0.2 s = 10 s
            if not any(
                sid in self.sessions
                and self.sessions[sid].pty.poll() is None
                for sid in ids
            ):
                break
            await asyncio.sleep(0.2)
        # Let _monitor_process / _on_session_exit callbacks finish
        await asyncio.sleep(0.3)

        # Phase 3 — force-remove anything still alive
        for sid in list(self.sessions.keys()):
            await self.remove(sid)
