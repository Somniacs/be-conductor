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
import time
import uuid as _uuid
from pathlib import Path
from typing import Dict, Optional

from be_conductor.notifications.manager import (
    NotificationManager, SessionNotifier, _DEFAULT_PATTERNS,
)
from be_conductor.sessions.session import Session, _ANSI_RE
from be_conductor.utils import config as cfg
from be_conductor.utils.config import SESSIONS_DIR, ensure_dirs

log = logging.getLogger(__name__)

SPAWN_CONTEXT_DIR = Path(cfg.CONDUCTOR_DIR) / "spawn-context"
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _lookup_claude_session_uuid(session_name: str, cwd: str) -> str | None:
    """Look up a Claude Code session UUID by its display name.

    Searches ``~/.claude/projects/<project>/`` for JSONL files whose
    ``custom-title`` or ``agent-name`` entry matches *session_name*.
    Returns the most recently modified match, or ``None``.
    """
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None
    # Claude encodes the CWD as the project dir name (/ → -)
    project_name = cwd.replace("/", "-")
    project_path = claude_dir / project_name
    if not project_path.is_dir():
        return None
    found: list[tuple[str, float]] = []
    for jsonl in project_path.glob("*.jsonl"):
        uuid = jsonl.stem
        if not _UUID_RE.match(uuid):
            continue
        try:
            with open(jsonl) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        etype = entry.get("type", "")
                        if etype in ("custom-title", "agent-name"):
                            title = entry.get("customTitle") or entry.get("agentName", "")
                            if title == session_name:
                                found.append((uuid, jsonl.stat().st_mtime))
                                break
                    except Exception:
                        pass
        except Exception:
            pass
    if found:
        # Most recently modified file wins
        return max(found, key=lambda x: x[1])[0]
    return None


async def _wait_for_file(path: Path, timeout: float = 60,
                         interval: float = 1.0) -> bool:
    """Poll until *path* exists and is non-empty, or *timeout* elapses."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if path.exists() and path.stat().st_size > 0:
            return True
        await asyncio.sleep(interval)
    return False


async def _deliver_context(session: "Session", message: str,
                           context_file: Path | None = None,
                           timeout: float = 15.0):
    """Wait for the agent to initialise, then type *message* into the PTY.

    If *context_file* is given, delete it after delivery so spawn-context
    doesn't accumulate stale files.
    """
    start = time.monotonic()
    while not session.buffer and session.status == "running":
        if time.monotonic() - start > timeout:
            break
        await asyncio.sleep(0.3)
    # Brief extra settle time for the prompt to fully render.
    await asyncio.sleep(0.5)
    if session.status == "running":
        session.send_input(message)
        # Give the agent time to read the file before deleting.
        if context_file:
            await asyncio.sleep(5)
            try:
                context_file.unlink(missing_ok=True)
            except Exception:
                pass


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
        self._shutting_down = False
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
        """Load persisted session metadata from disk on startup.

        Every session file is loaded so nothing is silently lost across
        restarts.  Sessions still marked ``running`` or ``stopping``
        represent a previous unclean shutdown — the process is long gone.
        If the command contains a ``--resume <id>`` flag from a previous
        resume, we recover the token so the session can be resumed again.
        """
        import re as _re
        for path in SESSIONS_DIR.glob("*.json"):
            if path.name.endswith(".history.json"):
                continue
            try:
                meta = json.loads(path.read_text())
                status = meta.get("status", "")

                # Recover sessions left in running/stopping state after a crash
                if status in ("running", "starting", "stopping"):
                    cmd = meta.get("command", "")
                    flag = meta.get("resume_flag", "--resume")
                    m = _re.search(rf'{_re.escape(flag)}\s+(\S+)', cmd)
                    if m:
                        meta["resume_id"] = m.group(1).strip('"').strip("'")
                    meta["status"] = "exited"
                    path.write_text(json.dumps(meta))

                # PTY sessions: if resume_id is a name (not UUID), look up
                # the real UUID from Claude's session storage.
                rid = meta.get("resume_id", "")
                if rid and not _UUID_RE.match(rid):
                    uuid = _lookup_claude_session_uuid(
                        rid, meta.get("cwd", ""))
                    if uuid:
                        meta["resume_id"] = uuid
                        path.write_text(json.dumps(meta))

                self.resumable[meta["id"]] = meta
            except Exception:
                pass

        # Dedup: if multiple sessions share the same resume_id, keep the
        # newest (by start_time) and clear the others' resume_id so they
        # start fresh on resume instead of conflicting.
        rid_owners: dict[str, list[str]] = {}
        for sid, meta in self.resumable.items():
            rid = meta.get("resume_id")
            if rid:
                rid_owners.setdefault(rid, []).append(sid)
        for rid, sids in rid_owners.items():
            if len(sids) > 1:
                # Keep the one with the latest start_time
                sids.sort(key=lambda s: self.resumable[s].get("start_time", 0),
                          reverse=True)
                keeper = sids[0]
                for dup in sids[1:]:
                    log.warning(
                        "Duplicate resume_id %s: keeping '%s', clearing '%s'",
                        rid[:12], keeper, dup)
                    self.resumable[dup]["resume_id"] = None
                    # Persist the change
                    dup_path = SESSIONS_DIR / f"{dup}.json"
                    if dup_path.exists():
                        try:
                            dup_path.write_text(json.dumps(self.resumable[dup]))
                        except Exception:
                            pass

    async def _on_session_exit(self, session_id: str):
        """Called when a session's process exits."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        # "Forget" mode: delete everything without saving resume data.
        is_agent = getattr(session, 'session_type', 'pty') == 'agent'
        if getattr(session, '_forget', False):
            if is_agent:
                # Agent sessions: delete both metadata and history
                meta_path = SESSIONS_DIR / f"{session_id}.json"
                meta_path.unlink(missing_ok=True)
                history_path = SESSIONS_DIR / f"{session_id}.history.json"
                history_path.unlink(missing_ok=True)
            else:
                self._delete_metadata(session_id)
            return

        # Worktree stays active on exit — user must explicitly finalize.
        # No auto-commit here; the worktree is just a working directory.

        # Keep session as resumable when it has a resume token, a worktree,
        # was explicitly stopped via graceful stop, or the server is
        # shutting down (so no session is silently lost on restart).
        was_graceful = getattr(session, '_was_graceful', False) or session.status == "stopping"
        is_agent = getattr(session, 'session_type', 'pty') == 'agent'
        if session.resume_id or session.worktree or was_graceful or self._shutting_down or is_agent:
            meta = session.to_dict()
            meta["status"] = "exited"
            self.resumable[session_id] = meta
            self._save_metadata_dict(meta)
        else:
            self._delete_metadata(session_id)

    async def create(self, name: str, command: str, cwd: str | None = None,
                     env: dict | None = None, rows: int | None = None,
                     cols: int | None = None, source: str | None = None,
                     worktree: bool = False,
                     session_type: str = "pty",
                     agent_options: dict | None = None,
                     session_id: str | None = None) -> Session:
        # Agent sessions get a unique UUID; PTY sessions keep name as ID
        # for backwards compatibility.  Callers (e.g. resume) can pass an
        # explicit session_id to reuse the old ID and its history file.
        if session_id:
            pass  # use caller-provided ID
        elif session_type == "agent":
            session_id = str(_uuid.uuid4())
        else:
            session_id = name

        # PTY sessions: check for name collision (PTY id == name)
        if session_type != "agent" and name in self.sessions:
            existing = self.sessions[name]
            if existing.status == "running":
                raise ValueError(f"Session '{name}' already exists and is running")
            else:
                await self.remove(name)

        # Agent sessions: check for running session with the same name
        if session_type == "agent":
            for sid, s in list(self.sessions.items()):
                if s.name == name and s.status == "running":
                    raise ValueError(f"Session '{name}' already exists and is running")

        # PTY: clear old resumable entry with the same name
        if session_type != "agent":
            self.resumable.pop(name, None)

        agent_cfg = self._agent_config_for(command)

        # Create worktree if requested
        worktree_info = None
        session_cwd = cwd
        if worktree and cwd:
            try:
                wt_info = self.worktree_manager.create(
                    session_name=name,
                    session_id=session_id,
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
            session_id=session_id,
            session_name=name,
            manager=self.notification_manager,
            patterns=notif_patterns,
        )

        if session_type == "agent":
            from be_conductor.sessions.agent_session import AgentSession
            session = AgentSession(
                name=name,
                prompt=command,
                session_id=session_id,
                cwd=session_cwd,
                on_exit=self._on_session_exit,
                env=env,
                worktree=worktree_info,
                notifier=notifier,
                agent_options=agent_options,
            )
        else:
            session = Session(
                name=name,
                command=command,
                session_id=session_id,
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
        self.sessions[session.id] = session
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
        meta = self.resumable.get(session_id)

        # Edge case: session just exited but _on_session_exit hasn't moved
        # it to self.resumable yet — check self.sessions as a fallback.
        if not meta:
            live = self.sessions.get(session_id)
            if live and live.status == "exited" and live.resume_id:
                meta = live.to_dict()
                self.sessions.pop(session_id, None)
                if hasattr(live, '_monitor_task') and live._monitor_task:
                    live._monitor_task.cancel()
                    try:
                        await live._monitor_task
                    except asyncio.CancelledError:
                        pass
                if hasattr(live, 'pty'):
                    live.pty.close()

        if not meta:
            raise ValueError(f"No resumable session '{session_id}'")

        # Guard: prevent two sessions from resuming the same Claude session.
        # If another running session already uses this resume_id, refuse.
        rid = meta.get("resume_id")
        if rid:
            for sid, s in self.sessions.items():
                if sid != session_id and getattr(s, 'resume_id', None) == rid and s.status == "running":
                    raise ValueError(
                        f"Cannot resume: Claude session {rid[:12]}… is already "
                        f"in use by '{sid}'"
                    )

        # Strip stale quotes from resume_id
        if meta.get("resume_id"):
            meta["resume_id"] = meta["resume_id"].strip('"').strip("'")
        has_resume_id = bool(meta.get("resume_id"))
        has_worktree = bool(meta.get("worktree"))

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
            # No resume token — restart original command in same CWD.
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

        # Create the resumed session (don't create a new worktree — reuse existing)
        # Only remove the resumable entry after successful creation so a
        # failed resume (e.g. command not found) doesn't lose the session.
        st = meta.get("session_type", "pty")
        agent_opts = None
        if st == "agent" and has_resume_id:
            # Agent sessions use the SDK's native resume, not shell flags.
            agent_opts = {"resume": meta["resume_id"]}
            command = "Resume session"  # display prompt (not sent to Claude)
        session = await self.create(meta["name"], command, cwd=cwd,
                                    rows=rows, cols=cols,
                                    session_type=st,
                                    agent_options=agent_opts,
                                    session_id=session_id)
        # Carry forward resume_id so fork works immediately
        if has_resume_id:
            session.resume_id = meta["resume_id"]

        # Clean up old resumable entry
        self.resumable.pop(session_id, None)
        # For agent sessions: rename old history file to new session ID
        # so the resumed session has the full conversation for UI replay.
        if st == "agent" and session.id != session_id:
            old_history = SESSIONS_DIR / f"{session_id}.history.json"
            new_history = SESSIONS_DIR / f"{session.id}.history.json"
            if old_history.exists() and not new_history.exists():
                old_history.rename(new_history)
        # Delete old metadata (and old history if rename failed or not agent)
        meta_path = SESSIONS_DIR / f"{session_id}.json"
        meta_path.unlink(missing_ok=True)
        old_hist = SESSIONS_DIR / f"{session_id}.history.json"
        old_hist.unlink(missing_ok=True)

        # Re-attach the worktree info
        if worktree_data:
            session.worktree = worktree_data
            self._save_metadata(session)
            from be_conductor.worktrees import state as wt_state
            wt_state.update_worktree(
                worktree_data["repo_path"], meta["name"], worktree_data
            )

        return session

    async def spawn(self, parent_id: str, name: str,
                    command: str | None = None, cwd: str | None = None,
                    worktree: bool = False, raw: bool = False,
                    context_lines: int = 500,
                    context_message: str | None = None,
                    rows: int | None = None, cols: int | None = None,
                    source: str | None = None,
                    on_timeout=None) -> Session:
        """Create a new session that inherits context from *parent_id*.

        By default, asks the parent session to write a summary to a
        context file (summarize mode).  If *raw* is True, the parent's
        terminal buffer is extracted and written directly instead.

        *on_timeout* — optional async callback invoked when the summary
        wait times out.  Must return ``"wait"`` (extend by another 120 s)
        or ``"raw"`` (fall back to raw buffer immediately).

        The new session receives a short prompt pointing it at the
        context file once its agent has initialised.
        """
        parent = self.get(parent_id)
        if not parent or parent.status != "running":
            raise ValueError("Source session not found or not running")

        effective_cwd = cwd or parent.live_cwd or parent.cwd
        st = getattr(parent, 'session_type', 'pty')

        # --- Agent sessions: always use SDK fork ---
        if st == "agent":
            if not parent.resume_id:
                raise ValueError("Cannot clone: session has no resume ID yet (send a message first)")
            from claude_agent_sdk._internal.session_mutations import fork_session as _fork
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: _fork(parent.resume_id, directory=effective_cwd, title=name)
            )
            session = await self.create(
                name, "claude", cwd=effective_cwd,
                rows=rows, cols=cols, source=source, worktree=worktree,
                session_type="agent",
                agent_options={"resume": result.session_id},
            )
            return session

        # --- PTY sessions: try CLI fork (UUID only), fall back to legacy ---
        if parent.resume_id and _UUID_RE.match(parent.resume_id):
            try:
                fork_cmd = f"claude --resume {parent.resume_id} --fork-session"
                session = await self.create(
                    name, fork_cmd, cwd=effective_cwd,
                    rows=rows, cols=cols, source=source, worktree=worktree,
                )
                return session
            except Exception as e:
                log.warning("CLI fork failed for '%s': %s; using legacy clone", parent_id, e)

        # --- Legacy clone (context file) ---
        SPAWN_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        context_file = SPAWN_CONTEXT_DIR / f"{name}.md"

        if raw:
            # Instant: dump stripped buffer to file.
            text = parent.get_buffer_text(max_lines=context_lines)
            context_file.write_text(text, encoding="utf-8")
        else:
            # Ask the parent agent to write a focused summary.
            prompt = (
                f"Please write a concise summary of our current session to "
                f"{context_file}. Include: current task/goal, key decisions, "
                f"important files, and current progress. Keep it under 100 "
                f"lines. Then continue your work.\n"
            )
            parent.send_input(prompt)
            ok = await _wait_for_file(context_file, timeout=120)
            while not ok:
                if on_timeout:
                    decision = await on_timeout()
                    if decision == "wait":
                        ok = await _wait_for_file(context_file, timeout=120)
                        continue
                # Fall back to raw buffer extraction.
                log.warning("Spawn summarize timed out for '%s'; "
                            "falling back to buffer extraction", parent_id)
                text = parent.get_buffer_text(max_lines=context_lines)
                context_file.write_text(text, encoding="utf-8")
                break

        # Inherit command and cwd from parent unless overridden.
        # Strip --resume flag so the clone starts a fresh session.
        effective_cmd = command or parent.command
        if effective_cmd and not command:
            import shlex
            try:
                parts = shlex.split(effective_cmd)
                cleaned = []
                skip_next = False
                for i, p in enumerate(parts):
                    if skip_next:
                        skip_next = False
                        continue
                    if p == '--resume' or p == '-r':
                        # Skip the flag and its argument
                        if i + 1 < len(parts) and not parts[i + 1].startswith('-'):
                            skip_next = True
                        continue
                    if p.startswith('--resume='):
                        continue
                    cleaned.append(p)
                effective_cmd = shlex.join(cleaned)
            except ValueError:
                pass  # malformed command — use as-is
        effective_cwd = cwd or parent.live_cwd or parent.cwd

        # For worktree spawns from a worktree parent, use the repo root.
        if worktree and parent.worktree:
            effective_cwd = parent.worktree.get("repo_path", effective_cwd)

        session = await self.create(
            name, effective_cmd, cwd=effective_cwd,
            rows=rows, cols=cols, source=source, worktree=worktree,
        )

        # Schedule context delivery once the agent is ready.
        msg = context_message or (
            f"Read the context from the previous session at {context_file} "
            f"and continue the work.\n"
        )
        asyncio.ensure_future(_deliver_context(session, msg, context_file=context_file))
        return session

    def get(self, session_id: str) -> Optional[Session]:
        """Look up a session by ID (primary) or name (fallback)."""
        session = self.sessions.get(session_id)
        if session:
            return session
        # Fallback: search by name (needed for PTY sessions where id == name,
        # and for any callers still passing a name for agent sessions).
        for s in self.sessions.values():
            if s.name == session_id:
                return s
        return None

    def list_all(self) -> list[dict]:
        # Detect dead agent sessions: _run_task finished but session still
        # in self.sessions.  Move them to resumable so they show as "exited".
        dead_agents = []
        for sid, session in self.sessions.items():
            if getattr(session, 'session_type', 'pty') == 'agent':
                task = getattr(session, '_run_task', None)
                if task and task.done():
                    dead_agents.append(sid)
        for sid in dead_agents:
            session = self.sessions.pop(sid, None)
            if session:
                session.status = "exited"
                meta = session.to_dict()
                meta["status"] = "exited"
                self.resumable[sid] = meta
                self._save_metadata_dict(meta)
                log.info("Cleaned up dead agent session: %s", sid)

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
        if not session:
            # Fallback: find by name (PTY compat)
            for sid, s in list(self.sessions.items()):
                if s.name == session_id:
                    session = self.sessions.pop(sid, None)
                    session_id = sid
                    break
        if session:
            await session.kill()
            await session.cleanup()
            # Try to extract resume info even after hard kill
            if hasattr(session, '_extract_resume_id'):
                session._extract_resume_id()
            if session.resume_id or session.worktree or self._shutting_down:
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
        session = self.get(session_id)
        if session and session.status in ("running", "starting"):
            session.status = "stopping"
            session.interrupt(timeout=cfg.GRACEFUL_STOP_TIMEOUT)

    def forget(self, session_id: str):
        """Gracefully stop a session and discard it without saving resume data.

        Like ``graceful_stop``, this sends SIGINT so the agent can clean up,
        but marks the session so that ``_on_session_exit`` deletes all
        metadata instead of saving it as resumable.
        """
        session = self.get(session_id)
        if session and session.status in ("running", "starting"):
            session._forget = True
            session.interrupt(timeout=cfg.GRACEFUL_STOP_TIMEOUT)

    def dismiss_resumable(self, session_id: str):
        """Remove a resumable entry without resuming it.

        Unlike ``_delete_metadata`` (which protects history files for
        internal callers), this is an explicit user action — delete
        everything: metadata JSON *and* history file.
        """
        self.resumable.pop(session_id, None)
        meta_path = SESSIONS_DIR / f"{session_id}.json"
        meta_path.unlink(missing_ok=True)
        history_path = SESSIONS_DIR / f"{session_id}.history.json"
        history_path.unlink(missing_ok=True)

    def clear_all_resumable(self) -> int:
        """Remove all resumable entries that have no worktree. Returns count removed."""
        to_remove = [
            sid for sid, meta in self.resumable.items()
            if not meta.get("worktree")
        ]
        for sid in to_remove:
            self.dismiss_resumable(sid)
        return len(to_remove)

    def _save_metadata(self, session: Session):
        path = SESSIONS_DIR / f"{session.id}.json"
        path.write_text(json.dumps(session.to_dict(), indent=2))

    def _save_metadata_dict(self, meta: dict):
        path = SESSIONS_DIR / f"{meta['id']}.json"
        path.write_text(json.dumps(meta, indent=2))

    def _delete_metadata(self, session_id: str):
        # If a history file exists, this is an agent session — NEVER delete.
        # History is too expensive to rebuild (can be 100K+ tokens of context).
        history_path = SESSIONS_DIR / f"{session_id}.history.json"
        if history_path.exists():
            log.warning("Refusing to delete agent session metadata: %s (history exists)", session_id)
            return
        path = SESSIONS_DIR / f"{session_id}.json"
        path.unlink(missing_ok=True)

    async def cleanup_all(self):
        """Gracefully stop all sessions, preserving resume tokens.

        Pre-save: Persist metadata for every running session immediately,
        so even a hard kill mid-shutdown won't lose session records.
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

        self._shutting_down = True

        # Pre-save — persist every running session before anything
        # destructive happens.  If shutdown is interrupted, these files
        # survive and _load_resumable() will pick them up on restart.
        for sid in ids:
            session = self.sessions.get(sid)
            if session:
                self._save_metadata(session)

        # Phase 0 — send ESC to break out of menus / thinking states (PTY only)
        for sid in ids:
            session = self.sessions.get(sid)
            if session and session.status in ("running", "starting") and hasattr(session, 'pty'):
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
                and hasattr(self.sessions[sid], 'pty')
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
