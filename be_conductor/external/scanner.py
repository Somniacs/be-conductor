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

"""Discover external AI agent sessions (Claude, Codex, Copilot, Gemini, Goose)."""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# --- Claude Code ---
_CLAUDE_DIR = Path.home() / ".claude"
_PROJECTS_DIR = _CLAUDE_DIR / "projects"
_CLAUDE_IDE_DIR = _CLAUDE_DIR / "ide"

# --- Codex ---
_CODEX_DIR = Path.home() / ".codex"
_CODEX_DB = _CODEX_DIR / "state_5.sqlite"

# --- Copilot CLI ---
_COPILOT_DIR = Path.home() / ".copilot"
_COPILOT_SESSIONS_DIR = _COPILOT_DIR / "session-state"
_COPILOT_IDE_DIR = _COPILOT_DIR / "ide"

# --- Gemini CLI ---
_GEMINI_TMP = Path.home() / ".gemini" / "tmp"

# --- Goose ---
_GOOSE_DIR = Path.home() / ".local" / "share" / "goose"
_GOOSE_DB = _GOOSE_DIR / "sessions" / "sessions.db"


def _parse_file_id(file_id: str) -> tuple[str, str]:
    """Split an agent-prefixed file_id into (agent, raw_id).

    Bare IDs without a prefix default to 'claude' for backward compat.
    """
    if "::" in file_id:
        agent, raw_id = file_id.split("::", 1)
        return agent, raw_id
    return "claude", file_id


class ExternalSessionScanner:
    """Scans local agent session stores and returns a unified session list."""

    def __init__(self):
        self._cache: list[dict] | None = None
        self._cache_time: float = 0
        self._cache_ttl: float = 10.0  # seconds

    def scan(self, project_filter: str | None = None,
             conductor_resume_ids: set[str] | None = None,
             agent_filter: str | None = None) -> list[dict]:
        """Return list of discovered external sessions, sorted by mtime desc.

        Args:
            project_filter: If set, only return sessions whose cwd starts with this path.
            conductor_resume_ids: Set of file_ids already running in Conductor (to exclude).
            agent_filter: If set, only return sessions for this agent.
        """
        now = time.time()
        if self._cache is not None and (now - self._cache_time) < self._cache_ttl:
            results = self._cache
        else:
            results = self._do_scan()
            self._cache = results
            self._cache_time = now

        # Filter out sessions already managed by Conductor
        if conductor_resume_ids:
            results = [r for r in results if r["file_id"] not in conductor_resume_ids]

        if agent_filter:
            results = [r for r in results if r.get("agent") == agent_filter]

        if project_filter:
            results = [r for r in results if r.get("project_path", "").startswith(project_filter)]

        return results[:50]

    def invalidate(self):
        """Force cache refresh on next scan."""
        self._cache = None

    def get_jsonl_path(self, file_id: str) -> Path | None:
        """Find the JSONL file for a given file_id."""
        agent, raw_id = _parse_file_id(file_id)

        if agent == "claude":
            return self._get_claude_jsonl_path(raw_id)
        elif agent == "codex":
            return self._get_codex_jsonl_path(raw_id)
        elif agent == "copilot":
            return self._get_copilot_jsonl_path(raw_id)
        # Gemini and Goose are not observable (no JSONL)
        return None

    def get_session_info(self, file_id: str) -> dict | None:
        """Get session info for a specific file_id (used by resume endpoint)."""
        agent, raw_id = _parse_file_id(file_id)

        if agent == "claude":
            path = self._get_claude_jsonl_path(raw_id)
            if not path:
                return None
            ide_locks = self._parse_ide_locks_dir(_CLAUDE_IDE_DIR)
            return self._parse_claude_session_file(path, ide_locks)
        elif agent == "codex":
            return self._get_codex_session_info(raw_id)
        elif agent == "copilot":
            return self._get_copilot_session_info(raw_id)
        elif agent == "gemini":
            return self._get_gemini_session_info(raw_id)
        elif agent == "goose":
            return self._get_goose_session_info(raw_id)
        return None

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _do_scan(self) -> list[dict]:
        """Perform the actual filesystem scan across all agents."""
        results = []
        results.extend(self._scan_claude())
        results.extend(self._scan_codex())
        results.extend(self._scan_copilot())
        results.extend(self._scan_gemini())
        results.extend(self._scan_goose())
        results.sort(key=lambda r: r["last_modified"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Claude Code
    # ------------------------------------------------------------------

    def _scan_claude(self) -> list[dict]:
        if not _PROJECTS_DIR.is_dir():
            return []

        ide_locks = self._parse_ide_locks_dir(_CLAUDE_IDE_DIR)
        results = []

        for project_dir in _PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            if "subagents" in project_dir.name:
                continue

            for jsonl_file in project_dir.glob("*.jsonl"):
                if "subagents" in str(jsonl_file):
                    continue
                try:
                    info = self._parse_claude_session_file(jsonl_file, ide_locks)
                    if info:
                        results.append(info)
                except Exception:
                    log.debug("Failed to parse %s", jsonl_file, exc_info=True)

        return results

    def _get_claude_jsonl_path(self, raw_id: str) -> Path | None:
        """Find the JSONL file for a Claude file_id across project dirs."""
        if not _PROJECTS_DIR.is_dir():
            return None
        for project_dir in _PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{raw_id}.jsonl"
            if candidate.is_file():
                return candidate
        return None

    def _parse_claude_session_file(self, path: Path, ide_locks: dict) -> dict | None:
        """Parse a single Claude Code JSONL session file and extract metadata."""
        try:
            stat = path.stat()
        except OSError:
            return None

        raw_id = path.stem  # UUID filename without .jsonl

        session_id = None
        slug = None
        cwd = None
        git_branch = None
        version = None

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                records_seen = 0
                for line in f:
                    if records_seen >= 15:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    rtype = record.get("type", "")
                    if rtype == "file-history-snapshot":
                        continue

                    records_seen += 1

                    if not session_id:
                        session_id = record.get("sessionId")
                    if not slug:
                        slug = record.get("slug")
                    if not cwd:
                        cwd = record.get("cwd")
                    if not git_branch:
                        git_branch = record.get("gitBranch")
                    if not version:
                        version = record.get("version")

                    if session_id and slug and cwd and git_branch and version:
                        break
        except OSError:
            return None

        if not cwd:
            cwd = self._decode_project_path(path.parent.name)

        project_path = cwd or self._decode_project_path(path.parent.name)

        is_live = False
        ide_name = None
        recently_modified = (time.time() - stat.st_mtime) < 120
        if cwd and ide_locks and recently_modified:
            for workspace_folder, lock_info in ide_locks.items():
                if cwd.startswith(workspace_folder) or workspace_folder.startswith(cwd):
                    is_live = True
                    ide_name = lock_info.get("ide_name")
                    break

        return {
            "file_id": f"claude::{raw_id}",
            "session_id": session_id or raw_id,
            "slug": slug or raw_id[:12],
            "cwd": cwd,
            "project_path": project_path,
            "git_branch": git_branch,
            "version": version,
            "last_modified": stat.st_mtime,
            "file_size": stat.st_size,
            "is_live": is_live,
            "ide_name": ide_name,
            "agent": "claude",
            "jsonl_path": str(path),
            "resume_command": f"claude --resume {raw_id}",
        }

    # ------------------------------------------------------------------
    # Codex
    # ------------------------------------------------------------------

    def _scan_codex(self) -> list[dict]:
        if not _CODEX_DB.is_file():
            return []

        results = []
        try:
            conn = sqlite3.connect(f"file:{_CODEX_DB}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, rollout_path, cwd, title, git_branch, updated_at, "
                "cli_version, archived FROM threads "
                "WHERE archived=0 ORDER BY updated_at DESC LIMIT 100"
            )
            now = time.time()
            for row in cursor.fetchall():
                try:
                    updated_at = row["updated_at"]
                    # Codex stores updated_at as Unix epoch seconds
                    mtime = float(updated_at)
                    rollout_path = row["rollout_path"]
                    jsonl_path = rollout_path if rollout_path and Path(rollout_path).is_file() else None
                    raw_id = row["id"]

                    results.append({
                        "file_id": f"codex::{raw_id}",
                        "session_id": raw_id,
                        "slug": (row["title"] or raw_id[:12])[:60],
                        "cwd": row["cwd"],
                        "project_path": row["cwd"],
                        "git_branch": row["git_branch"],
                        "version": row["cli_version"],
                        "last_modified": mtime,
                        "file_size": 0,
                        "is_live": (now - mtime) < 120,
                        "ide_name": None,
                        "agent": "codex",
                        "jsonl_path": jsonl_path,
                        "resume_command": "codex resume",
                    })
                except Exception:
                    log.debug("Failed to parse Codex thread %s", row["id"], exc_info=True)
            conn.close()
        except Exception:
            log.debug("Failed to scan Codex database", exc_info=True)

        return results

    def _get_codex_jsonl_path(self, raw_id: str) -> Path | None:
        """Look up the rollout JSONL path for a Codex thread."""
        if not _CODEX_DB.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{_CODEX_DB}?mode=ro", uri=True, timeout=5)
            cursor = conn.cursor()
            cursor.execute("SELECT rollout_path FROM threads WHERE id=?", (raw_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                p = Path(row[0])
                if p.is_file():
                    return p
        except Exception:
            log.debug("Failed to look up Codex JSONL for %s", raw_id, exc_info=True)
        return None

    def _get_codex_session_info(self, raw_id: str) -> dict | None:
        """Get session info for a specific Codex thread."""
        if not _CODEX_DB.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{_CODEX_DB}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, rollout_path, cwd, title, git_branch, updated_at, "
                "cli_version FROM threads WHERE id=?", (raw_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if not row:
                return None
            rollout_path = row["rollout_path"]
            jsonl_path = rollout_path if rollout_path and Path(rollout_path).is_file() else None
            return {
                "file_id": f"codex::{raw_id}",
                "session_id": raw_id,
                "slug": (row["title"] or raw_id[:12])[:60],
                "cwd": row["cwd"],
                "project_path": row["cwd"],
                "git_branch": row["git_branch"],
                "version": row["cli_version"],
                "last_modified": float(row["updated_at"]),
                "file_size": 0,
                "is_live": (time.time() - float(row["updated_at"])) < 120,
                "ide_name": None,
                "agent": "codex",
                "jsonl_path": jsonl_path,
                "resume_command": "codex resume",
            }
        except Exception:
            log.debug("Failed to get Codex session info for %s", raw_id, exc_info=True)
        return None

    # ------------------------------------------------------------------
    # Copilot CLI
    # ------------------------------------------------------------------

    def _scan_copilot(self) -> list[dict]:
        if not _COPILOT_SESSIONS_DIR.is_dir():
            return []

        ide_locks = self._parse_ide_locks_dir(_COPILOT_IDE_DIR)
        results = []

        try:
            import yaml
        except ImportError:
            log.debug("pyyaml not installed, skipping Copilot scan")
            return []

        for session_dir in _COPILOT_SESSIONS_DIR.iterdir():
            if not session_dir.is_dir():
                continue
            workspace_file = session_dir / "workspace.yaml"
            if not workspace_file.is_file():
                continue
            try:
                info = self._parse_copilot_session(session_dir, workspace_file, ide_locks)
                if info:
                    results.append(info)
            except Exception:
                log.debug("Failed to parse Copilot session %s", session_dir.name, exc_info=True)

        return results

    def _parse_copilot_session(self, session_dir: Path, workspace_file: Path, ide_locks: dict) -> dict | None:
        """Parse a Copilot session directory."""
        import yaml

        try:
            content = workspace_file.read_text(encoding="utf-8", errors="replace")
            meta = yaml.safe_load(content) or {}
        except Exception:
            return None

        raw_id = meta.get("id", session_dir.name)
        cwd = meta.get("cwd")
        summary = meta.get("summary")
        updated_at_str = meta.get("updated_at") or meta.get("created_at")

        # Parse timestamp — yaml.safe_load auto-converts ISO timestamps to datetime
        mtime = 0.0
        if updated_at_str:
            try:
                from datetime import datetime, timezone
                if isinstance(updated_at_str, datetime):
                    mtime = updated_at_str.timestamp()
                elif isinstance(updated_at_str, str):
                    dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                    mtime = dt.timestamp()
                else:
                    mtime = float(updated_at_str)
            except (ValueError, OSError, TypeError):
                pass

        # Fallback to workspace.yaml file mtime
        if mtime == 0.0:
            try:
                mtime = workspace_file.stat().st_mtime
            except OSError:
                pass

        events_file = session_dir / "events.jsonl"
        jsonl_path = str(events_file) if events_file.is_file() else None

        # Check liveness via IDE locks
        is_live = False
        ide_name = None
        recently_modified = (time.time() - mtime) < 120 if mtime else False
        if cwd and ide_locks and recently_modified:
            for workspace_folder, lock_info in ide_locks.items():
                if cwd.startswith(workspace_folder) or workspace_folder.startswith(cwd):
                    is_live = True
                    ide_name = lock_info.get("ide_name")
                    break

        return {
            "file_id": f"copilot::{raw_id}",
            "session_id": raw_id,
            "slug": (summary or raw_id[:12])[:60],
            "cwd": cwd,
            "project_path": cwd,
            "git_branch": None,
            "version": None,
            "last_modified": mtime,
            "file_size": 0,
            "is_live": is_live,
            "ide_name": ide_name,
            "agent": "copilot",
            "jsonl_path": jsonl_path,
            "resume_command": f"copilot --resume {raw_id}",
        }

    def _get_copilot_jsonl_path(self, raw_id: str) -> Path | None:
        """Find the events.jsonl for a Copilot session."""
        if not _COPILOT_SESSIONS_DIR.is_dir():
            return None
        session_dir = _COPILOT_SESSIONS_DIR / raw_id
        events_file = session_dir / "events.jsonl"
        if events_file.is_file():
            return events_file
        return None

    def _get_copilot_session_info(self, raw_id: str) -> dict | None:
        """Get session info for a specific Copilot session."""
        if not _COPILOT_SESSIONS_DIR.is_dir():
            return None
        session_dir = _COPILOT_SESSIONS_DIR / raw_id
        workspace_file = session_dir / "workspace.yaml"
        if not workspace_file.is_file():
            return None
        ide_locks = self._parse_ide_locks_dir(_COPILOT_IDE_DIR)
        return self._parse_copilot_session(session_dir, workspace_file, ide_locks)

    # ------------------------------------------------------------------
    # Gemini CLI (defensive stub)
    # ------------------------------------------------------------------

    def _scan_gemini(self) -> list[dict]:
        if not _GEMINI_TMP.is_dir():
            return []

        results = []
        try:
            for hash_dir in _GEMINI_TMP.iterdir():
                if not hash_dir.is_dir():
                    continue
                chats_dir = hash_dir / "chats"
                if not chats_dir.is_dir():
                    continue
                for session_file in chats_dir.glob("session-*.json"):
                    try:
                        info = self._parse_gemini_session(session_file)
                        if info:
                            results.append(info)
                    except Exception:
                        log.debug("Failed to parse Gemini session %s", session_file, exc_info=True)
        except Exception:
            log.debug("Failed to scan Gemini sessions", exc_info=True)

        return results

    def _parse_gemini_session(self, path: Path) -> dict | None:
        """Parse a Gemini CLI session JSON file."""
        try:
            stat = path.stat()
        except OSError:
            return None

        raw_id = path.stem  # e.g. "session-abc123"

        # Try to extract basic metadata from the JSON
        cwd = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                # Read only first 4KB for metadata
                head = f.read(4096)
                data = json.loads(head)
                if isinstance(data, dict):
                    cwd = data.get("cwd")
        except (json.JSONDecodeError, OSError):
            pass

        return {
            "file_id": f"gemini::{raw_id}",
            "session_id": raw_id,
            "slug": raw_id[:30],
            "cwd": cwd,
            "project_path": cwd,
            "git_branch": None,
            "version": None,
            "last_modified": stat.st_mtime,
            "file_size": stat.st_size,
            "is_live": (time.time() - stat.st_mtime) < 120,
            "ide_name": None,
            "agent": "gemini",
            "jsonl_path": None,  # JSON, not JSONL — not observable
            "resume_command": f"gemini --resume {raw_id}",
        }

    def _get_gemini_session_info(self, raw_id: str) -> dict | None:
        """Get session info for a specific Gemini session."""
        if not _GEMINI_TMP.is_dir():
            return None
        for hash_dir in _GEMINI_TMP.iterdir():
            if not hash_dir.is_dir():
                continue
            chats_dir = hash_dir / "chats"
            if not chats_dir.is_dir():
                continue
            candidate = chats_dir / f"{raw_id}.json"
            if candidate.is_file():
                return self._parse_gemini_session(candidate)
        return None

    # ------------------------------------------------------------------
    # Goose (defensive stub)
    # ------------------------------------------------------------------

    def _scan_goose(self) -> list[dict]:
        if not _GOOSE_DIR.is_dir():
            return []

        results = []

        # Try SQLite database first
        if _GOOSE_DB.is_file():
            try:
                results = self._scan_goose_db()
            except Exception:
                log.debug("Failed to scan Goose database", exc_info=True)

        # Fallback: look for JSONL files in the sessions directory
        if not results:
            sessions_dir = _GOOSE_DIR / "sessions"
            if sessions_dir.is_dir():
                for jsonl_file in sessions_dir.glob("*.jsonl"):
                    try:
                        stat = jsonl_file.stat()
                        raw_id = jsonl_file.stem
                        results.append({
                            "file_id": f"goose::{raw_id}",
                            "session_id": raw_id,
                            "slug": raw_id[:30],
                            "cwd": None,
                            "project_path": None,
                            "git_branch": None,
                            "version": None,
                            "last_modified": stat.st_mtime,
                            "file_size": stat.st_size,
                            "is_live": (time.time() - stat.st_mtime) < 120,
                            "ide_name": None,
                            "agent": "goose",
                            "jsonl_path": str(jsonl_file),
                            "resume_command": f"goose session --resume {raw_id}",
                        })
                    except Exception:
                        log.debug("Failed to parse Goose session %s", jsonl_file, exc_info=True)

        return results

    def _scan_goose_db(self) -> list[dict]:
        """Scan the Goose SQLite sessions database."""
        results = []
        conn = sqlite3.connect(f"file:{_GOOSE_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Goose schema may vary — try common columns
        try:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            if not cursor.fetchone():
                conn.close()
                return []
            cursor.execute("SELECT * FROM sessions ORDER BY rowid DESC LIMIT 100")
            for row in cursor.fetchall():
                row_dict = dict(row)
                raw_id = str(row_dict.get("id", row_dict.get("session_id", "")))
                if not raw_id:
                    continue
                mtime = float(row_dict.get("updated_at", row_dict.get("created_at", 0)))
                results.append({
                    "file_id": f"goose::{raw_id}",
                    "session_id": raw_id,
                    "slug": str(row_dict.get("description", raw_id[:12]))[:60],
                    "cwd": row_dict.get("working_directory"),
                    "project_path": row_dict.get("working_directory"),
                    "git_branch": None,
                    "version": None,
                    "last_modified": mtime,
                    "file_size": 0,
                    "is_live": (time.time() - mtime) < 120 if mtime else False,
                    "ide_name": None,
                    "agent": "goose",
                    "jsonl_path": None,
                    "resume_command": f"goose session --resume {raw_id}",
                })
        except Exception:
            log.debug("Failed to query Goose sessions table", exc_info=True)
        conn.close()
        return results

    def _get_goose_session_info(self, raw_id: str) -> dict | None:
        """Get session info for a specific Goose session."""
        # Try JSONL files first
        sessions_dir = _GOOSE_DIR / "sessions"
        if sessions_dir.is_dir():
            candidate = sessions_dir / f"{raw_id}.jsonl"
            if candidate.is_file():
                try:
                    stat = candidate.stat()
                    return {
                        "file_id": f"goose::{raw_id}",
                        "session_id": raw_id,
                        "slug": raw_id[:30],
                        "cwd": None,
                        "project_path": None,
                        "git_branch": None,
                        "version": None,
                        "last_modified": stat.st_mtime,
                        "file_size": stat.st_size,
                        "is_live": False,
                        "ide_name": None,
                        "agent": "goose",
                        "jsonl_path": str(candidate),
                        "resume_command": f"goose session --resume {raw_id}",
                    }
                except OSError:
                    pass
        # Try database
        if _GOOSE_DB.is_file():
            try:
                conn = sqlite3.connect(f"file:{_GOOSE_DB}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM sessions WHERE id=? OR session_id=?", (raw_id, raw_id))
                row = cursor.fetchone()
                conn.close()
                if row:
                    row_dict = dict(row)
                    mtime = float(row_dict.get("updated_at", row_dict.get("created_at", 0)))
                    return {
                        "file_id": f"goose::{raw_id}",
                        "session_id": raw_id,
                        "slug": str(row_dict.get("description", raw_id[:12]))[:60],
                        "cwd": row_dict.get("working_directory"),
                        "project_path": row_dict.get("working_directory"),
                        "git_branch": None,
                        "version": None,
                        "last_modified": mtime,
                        "file_size": 0,
                        "is_live": False,
                        "ide_name": None,
                        "agent": "goose",
                        "jsonl_path": None,
                        "resume_command": f"goose session --resume {raw_id}",
                    }
            except Exception:
                log.debug("Failed to look up Goose session %s", raw_id, exc_info=True)
        return None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ide_locks_dir(ide_dir: Path) -> dict:
        """Read *.lock files from a directory and return workspace -> lock info map.

        Lock files contain one or more concatenated JSON objects (not an array).
        Only returns entries with live PIDs.
        """
        result = {}
        if not ide_dir or not ide_dir.is_dir():
            return result

        for lock_file in ide_dir.glob("*.lock"):
            try:
                content = lock_file.read_text(encoding="utf-8", errors="replace")
                objects = ExternalSessionScanner._parse_concatenated_json(content)
                for obj in objects:
                    pid = obj.get("pid")
                    if not pid or not ExternalSessionScanner._is_pid_alive(pid):
                        continue
                    ide_name = obj.get("ideName", "IDE")
                    for folder in obj.get("workspaceFolders", []):
                        result[folder] = {"ide_name": ide_name, "pid": pid}
            except Exception:
                log.debug("Failed to parse lock file %s", lock_file, exc_info=True)

        return result

    @staticmethod
    def _parse_concatenated_json(text: str) -> list[dict]:
        """Parse concatenated JSON objects from a string.

        Handles the format where multiple JSON objects are concatenated
        without separators (e.g. `{...}{...}`).
        """
        objects = []
        text = text.strip()
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(text):
            while pos < len(text) and text[pos] in " \t\n\r":
                pos += 1
            if pos >= len(text):
                break
            try:
                obj, end = decoder.raw_decode(text, pos)
                objects.append(obj)
                pos = end
            except json.JSONDecodeError:
                break
        return objects

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    @staticmethod
    def _decode_project_path(dir_name: str) -> str | None:
        """Reverse the Claude project path encoding (hyphens -> slashes)."""
        if not dir_name.startswith("-"):
            return None
        return dir_name.replace("-", "/")
