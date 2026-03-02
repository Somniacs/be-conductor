# conductor — Local orchestration for terminal sessions.
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

"""Discover external Claude Code sessions from ~/.claude/projects/ JSONL files."""

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_CLAUDE_DIR = Path.home() / ".claude"
_PROJECTS_DIR = _CLAUDE_DIR / "projects"
_IDE_DIR = _CLAUDE_DIR / "ide"


class ExternalSessionScanner:
    """Scans ~/.claude/projects/ for JSONL session files and determines liveness."""

    def __init__(self):
        self._cache: list[dict] | None = None
        self._cache_time: float = 0
        self._cache_ttl: float = 10.0  # seconds

    def scan(self, project_filter: str | None = None,
             conductor_resume_ids: set[str] | None = None) -> list[dict]:
        """Return list of discovered external sessions, sorted by mtime desc.

        Args:
            project_filter: If set, only return sessions whose cwd starts with this path.
            conductor_resume_ids: Set of file_ids already running in Conductor (to exclude).
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

        if project_filter:
            results = [r for r in results if r.get("project_path", "").startswith(project_filter)]

        return results[:50]

    def invalidate(self):
        """Force cache refresh on next scan."""
        self._cache = None

    def get_jsonl_path(self, file_id: str) -> Path | None:
        """Find the JSONL file for a given file_id across all project dirs."""
        if not _PROJECTS_DIR.is_dir():
            return None
        for project_dir in _PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{file_id}.jsonl"
            if candidate.is_file():
                return candidate
        return None

    def get_session_info(self, file_id: str) -> dict | None:
        """Get session info for a specific file_id (used by resume endpoint)."""
        path = self.get_jsonl_path(file_id)
        if not path:
            return None
        ide_locks = self._parse_ide_locks()
        return self._parse_session_file(path, ide_locks)

    def _do_scan(self) -> list[dict]:
        """Perform the actual filesystem scan."""
        if not _PROJECTS_DIR.is_dir():
            return []

        ide_locks = self._parse_ide_locks()
        results = []

        for project_dir in _PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            # Skip subagent directories
            if "subagents" in project_dir.name:
                continue

            for jsonl_file in project_dir.glob("*.jsonl"):
                # Skip subagent files
                if "subagents" in str(jsonl_file):
                    continue
                try:
                    info = self._parse_session_file(jsonl_file, ide_locks)
                    if info:
                        results.append(info)
                except Exception:
                    log.debug("Failed to parse %s", jsonl_file, exc_info=True)

        # Sort by last_modified descending
        results.sort(key=lambda r: r["last_modified"], reverse=True)
        return results

    def _parse_session_file(self, path: Path, ide_locks: dict) -> dict | None:
        """Parse a single JSONL session file and extract metadata."""
        try:
            stat = path.stat()
        except OSError:
            return None

        file_id = path.stem  # UUID filename without .jsonl

        # Read first few KB to extract metadata from early records
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

                    # Stop early if we have everything
                    if session_id and slug and cwd and git_branch and version:
                        break
        except OSError:
            return None

        if not cwd:
            # Try to decode from directory name
            cwd = self._decode_project_path(path.parent.name)

        project_path = cwd or self._decode_project_path(path.parent.name)

        # Determine if this is a live IDE session
        is_live = False
        ide_name = None
        if cwd and ide_locks:
            for workspace_folder, lock_info in ide_locks.items():
                if cwd.startswith(workspace_folder) or workspace_folder.startswith(cwd):
                    is_live = True
                    ide_name = lock_info.get("ide_name")
                    break

        return {
            "file_id": file_id,
            "session_id": session_id or file_id,
            "slug": slug or file_id[:12],
            "cwd": cwd,
            "project_path": project_path,
            "git_branch": git_branch,
            "version": version,
            "last_modified": stat.st_mtime,
            "file_size": stat.st_size,
            "is_live": is_live,
            "ide_name": ide_name,
        }

    def _parse_ide_locks(self) -> dict:
        """Read ~/.claude/ide/*.lock files and return workspace → lock info map.

        Lock files contain one or more concatenated JSON objects (not an array).
        Only returns entries with live PIDs.
        """
        result = {}
        if not _IDE_DIR.is_dir():
            return result

        for lock_file in _IDE_DIR.glob("*.lock"):
            try:
                content = lock_file.read_text(encoding="utf-8", errors="replace")
                # Parse potentially concatenated JSON objects
                objects = self._parse_concatenated_json(content)
                for obj in objects:
                    pid = obj.get("pid")
                    if not pid or not self._is_pid_alive(pid):
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
            # Skip whitespace
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
        """Reverse the Claude project path encoding (hyphens → slashes)."""
        if not dir_name.startswith("-"):
            return None
        return dir_name.replace("-", "/")
