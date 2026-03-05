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

"""PTY-backed terminal session with buffering and WebSocket broadcast."""

import asyncio
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Set

# Regex to strip ANSI escape sequences from terminal output.
_ANSI_RE = re.compile(
    r'\x1b'           # ESC
    r'(?:'
    r'\[[\x20-\x3f]*[a-zA-Z@-~]'  # CSI sequences incl. private modes (e.g. \e[?2026h)
    r'|\][^\x07]*\x07'     # OSC sequences  (e.g. \e]0;title\a)
    r'|[()][AB012]'        # charset select
    r'|[>=<]'              # keypad modes
    r'|#[0-9]'             # line attrs
    r'|.'                  # two-char sequences
    r')'
)

# Default pattern to find `--resume <id>` in Claude Code exit output.
# Used as fallback when no per-command resume_pattern is configured.
_DEFAULT_RESUME_RE = re.compile(r'--resume\s+(\S+)')

import shutil

from be_conductor.proxy.pty_wrapper import PTYProcess
from be_conductor.utils import config as cfg
from be_conductor.utils.config import UPLOADS_DIR

logger = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"


class Session:
    """A single managed terminal session backed by a PTY."""

    def __init__(self, name: str, command: str, session_id: str | None = None,
                 cwd: str | None = None, on_exit=None, env: dict | None = None,
                 resume_pattern: str | None = None,
                 resume_flag: str | None = None,
                 resume_command: str | None = None,
                 stop_sequence: list[str] | None = None,
                 worktree: dict | None = None,
                 notifier=None):
        self.id = session_id or name
        self.name = name
        self.command = command
        self.cwd = cwd
        self.worktree: dict | None = worktree  # WorktreeInfo as dict (if worktree-backed)
        self.pty = PTYProcess(command, cwd=cwd, env=env)
        self.buffer = bytearray()
        self.subscribers: Set[asyncio.Queue] = set()
        self.status = "starting"
        self.pid: int | None = None
        self.start_time: float | None = None
        self.created_at: str | None = None
        self.exit_code: int | None = None
        self.resume_id: str | None = None
        self.resume_flag: str | None = resume_flag
        self.resume_command: str | None = resume_command
        self._resume_re = re.compile(resume_pattern) if resume_pattern else None
        self._stop_sequence: list[str] | None = stop_sequence
        self.rows: int = 24
        self.cols: int = 80
        self.resize_source: str | None = None
        self.resize_owner_id: str | None = None     # client_id of the CLI resize authority
        self.browser_resize_owner_id: str | None = None  # client_id of the browser resize authority
        self.cli_attach_count: int = 0               # number of CLI WebSocket connections
        self._monitor_task: asyncio.Task | None = None
        self._on_exit = on_exit
        self._reader_thread: threading.Thread | None = None
        self._queue_overflow_warned: bool = False
        self._notifier = notifier  # SessionNotifier instance (optional)

    async def start(self, rows: int = 24, cols: int = 80):
        self.pty.spawn(rows=rows, cols=cols)
        self.pid = self.pty.pid
        self.start_time = time.time()
        self.created_at = datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat()
        self.status = "running"

        if _IS_WIN:
            # Windows: ConPTY doesn't expose a file descriptor, so we
            # read in a background thread and push data to the event loop.
            self._loop = asyncio.get_event_loop()
            self._reader_thread = threading.Thread(
                target=self._win_read_loop, daemon=True
            )
            self._reader_thread.start()
        else:
            # Unix: register the PTY master fd with the event loop.
            loop = asyncio.get_event_loop()
            loop.add_reader(self.pty.master_fd, self._on_readable)

        self._monitor_task = asyncio.create_task(self._monitor_process())

    # -- Unix reader (event-loop based) ------------------------------------

    def _on_readable(self):
        try:
            data = os.read(self.pty.master_fd, 65536)
            if data:
                self._append_buffer(data)
                self._broadcast(data)
        except OSError:
            # EIO means the slave side closed (process exited).
            # Unregister immediately to avoid a tight spin in the event loop.
            try:
                asyncio.get_event_loop().remove_reader(self.pty.master_fd)
            except Exception:
                pass

    # -- Windows reader (thread-based) -------------------------------------

    def _win_read_loop(self):
        """Background thread that reads from ConPTY and feeds the event loop."""
        while not self.pty.closed:
            try:
                data = self.pty.read()
                if data:
                    self._loop.call_soon_threadsafe(self._append_buffer, data)
                    self._loop.call_soon_threadsafe(self._broadcast, data)
                else:
                    time.sleep(0.01)
            except OSError:
                break
            except Exception:
                break

    # -- Buffer & broadcast ------------------------------------------------

    def _append_buffer(self, data: bytes):
        self.buffer.extend(data)
        if len(self.buffer) > cfg.BUFFER_MAX_BYTES:
            excess = len(self.buffer) - cfg.BUFFER_MAX_BYTES
            del self.buffer[:excess]
        # Feed the notifier so it can detect when the agent needs attention.
        if self._notifier:
            self._notifier.on_output(data, self.buffer)

    def _broadcast(self, data: bytes):
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # Never drop data — coalesce pending items to make room.
                # Dropping bytes breaks ANSI escape sequences and garbles
                # TUI output (e.g. Claude Code's agent progress tree).
                merged = bytearray()
                try:
                    while not queue.empty():
                        merged.extend(queue.get_nowait())
                except asyncio.QueueEmpty:
                    pass
                merged.extend(data)
                queue.put_nowait(bytes(merged))

    def _broadcast_close(self):
        """Send None sentinel to all subscribers to signal session end."""
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self.subscribers.discard(queue)

    def get_buffer(self) -> bytes:
        return bytes(self.buffer)

    def send_input(self, text: str):
        self.pty.write(text.encode())

    def send_input_bytes(self, data: bytes):
        self.pty.write(data)

    def resize(self, rows: int, cols: int, source: str | None = None,
               client_id: str | None = None):
        """Resize the PTY with priority enforcement.

        Priority: CLI owner > other CLIs > browser owner > other browsers.
        """
        if source == "cli":
            if self.resize_owner_id and client_id and client_id != self.resize_owner_id:
                return  # Not the CLI owner — ignore
            if not self.resize_owner_id and client_id:
                self.resize_owner_id = client_id  # First CLI becomes owner
        elif source == "browser":
            if self.cli_attach_count > 0:
                return  # CLI connected — browser can't resize
            if self.browser_resize_owner_id and client_id and client_id != self.browser_resize_owner_id:
                return  # Not the browser owner — ignore
            if not self.browser_resize_owner_id and client_id:
                self.browser_resize_owner_id = client_id
        self.rows = rows
        self.cols = cols
        if source:
            self.resize_source = source
        self.pty.resize(rows, cols)

    def cli_connected(self, client_id: str):
        """Track a CLI WebSocket connection."""
        self.cli_attach_count += 1
        if not self.resize_owner_id:
            self.resize_owner_id = client_id

    def cli_disconnected(self, client_id: str):
        """Track a CLI WebSocket disconnection."""
        self.cli_attach_count = max(0, self.cli_attach_count - 1)
        if client_id == self.resize_owner_id:
            self.resize_owner_id = None  # Owner left — next CLI resize will claim
            if self.cli_attach_count == 0 and not self.browser_resize_owner_id:
                self.resize_source = None  # No CLI or browser owner → fresh start

    def browser_connected(self, client_id: str):
        """Track a browser WebSocket connection."""
        if not self.browser_resize_owner_id:
            self.browser_resize_owner_id = client_id

    def browser_disconnected(self, client_id: str):
        """Track a browser WebSocket disconnection."""
        if client_id == self.browser_resize_owner_id:
            self.browser_resize_owner_id = None
            if self.cli_attach_count == 0:
                self.resize_source = None

    def _cleanup_uploads(self):
        """Remove the session's upload directory."""
        upload_dir = UPLOADS_DIR / self.id
        if upload_dir.is_dir():
            shutil.rmtree(upload_dir, ignore_errors=True)

    def _extract_resume_id(self):
        """Scan the tail of the terminal buffer for a resume token.

        Uses the per-command ``resume_pattern`` if configured, otherwise
        falls back to the default ``--resume <id>`` pattern so existing
        Claude Code sessions keep working.

        If ``resume_command`` is set (e.g. ``codex resume --last``), the
        session is always resumable — no pattern matching needed.
        """
        # Agents that manage their own session history (e.g. Codex) don't
        # print a resume token.  Mark them as always-resumable.
        if self.resume_command:
            self.resume_id = "__always__"
            return

        pattern = self._resume_re or _DEFAULT_RESUME_RE
        try:
            # Only inspect the last 4 KB — the resume line is near the end.
            tail = bytes(self.buffer[-4096:]).decode("utf-8", errors="replace")
            clean = _ANSI_RE.sub("", tail)
            match = pattern.search(clean)
            if match:
                self.resume_id = match.group(1)
        except Exception:
            pass

    async def _monitor_process(self):
        while self.pty.poll() is None:
            await asyncio.sleep(0.5)
        self.exit_code = self.pty.poll()

        # Don't set status to "exited" yet — first drain remaining PTY
        # data and extract any resume token.  During this brief window
        # the session keeps its current status ("running" or "stopping")
        # so the frontend never sees "exited" with resume_id=None.

        # Let the event loop process any pending readable callbacks so
        # late output (e.g. a resume token printed during shutdown) lands
        # in the buffer before we look for it.
        await asyncio.sleep(0.1)

        if not _IS_WIN:
            # Drain any remaining data from the PTY fd — the resume token
            # is often the very last thing an agent prints.
            try:
                while True:
                    data = os.read(self.pty.master_fd, 65536)
                    if not data:
                        break
                    self._append_buffer(data)
                    self._broadcast(data)
            except OSError:
                pass
            try:
                asyncio.get_event_loop().remove_reader(self.pty.master_fd)
            except Exception:
                pass

        self._extract_resume_id()

        # Stop notification scanning.
        if self._notifier:
            self._notifier.cancel()

        # Now mark as exited — resume_id is already set (if found).
        self.status = "exited"

        self._broadcast(b"\r\n[Process exited]\r\n")
        self._broadcast_close()
        self.pty.close()
        self._cleanup_uploads()
        if self._on_exit:
            await self._on_exit(self.id)

    def interrupt(self, timeout: float = 30.0):
        """Gracefully stop the session.

        If a ``stop_sequence`` is configured (e.g. ``["\\x03", "/exit\\n"]``
        for Claude Code), each string is written to the PTY with a short
        delay so the agent can process its own shutdown command and print
        a resume token.  Otherwise falls back to SIGINT.

        If the process hasn't exited after *timeout* seconds, it is killed.
        """
        self.status = "stopping"
        if self._stop_sequence:
            task = asyncio.ensure_future(self._send_stop_sequence())
            task.add_done_callback(self._log_task_exception)
        elif _IS_WIN:
            self.pty.write(b'\x03')
        else:
            self.pty.interrupt_pg()
        # Escalate to SIGTERM if the process doesn't exit in time.
        task = asyncio.ensure_future(self._escalate_kill(timeout))
        task.add_done_callback(self._log_task_exception)

    async def _send_stop_sequence(self):
        """Write each item in the stop sequence to the PTY with delays.

        Uses a longer delay after the first item (e.g. Ctrl-C → wait for
        the agent to return to its prompt) and short delays between
        subsequent items (e.g. command text → Enter key).
        """
        for i, item in enumerate(self._stop_sequence):
            if self.pty.closed or self.status == "exited":
                break
            try:
                self.pty.write(item.encode())
            except OSError:
                break
            if i < len(self._stop_sequence) - 1:
                # Pause after first item (interrupt → wait for prompt).
                # Shorter pause between subsequent items.
                await asyncio.sleep(1.0 if i == 0 else 0.2)

    @staticmethod
    def _log_task_exception(task: asyncio.Task):
        """Done-callback for fire-and-forget tasks — log instead of crash."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Background task failed: %s", exc, exc_info=exc)

    async def _escalate_kill(self, timeout: float):
        """Wait for *timeout* seconds, then SIGTERM if still running."""
        try:
            await asyncio.sleep(timeout)
            if self.status == "stopping" and self.pty.poll() is None:
                self.pty.kill()
        except Exception:
            logger.exception("Error in _escalate_kill")

    async def kill(self):
        # Run the blocking pty.kill() in a thread to avoid blocking the
        # event loop (it may wait briefly for process exit).
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.pty.kill)
        self.status = "killed"

        if not _IS_WIN:
            try:
                loop.remove_reader(self.pty.master_fd)
            except Exception:
                pass

        self._broadcast_close()
        self._cleanup_uploads()

    async def cleanup(self):
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self.pty.close()

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "pid": self.pid,
            "start_time": self.start_time,
            "created_at": self.created_at,
            "exit_code": self.exit_code,
            "cwd": self.cwd,
            "rows": self.rows,
            "cols": self.cols,
            "resize_source": self.resize_source,
            "resize_owner": self.resize_owner_id or self.browser_resize_owner_id,
            "cli_attach_count": self.cli_attach_count,
        }
        if self.resume_id:
            d["resume_id"] = self.resume_id
        if self.resume_flag:
            d["resume_flag"] = self.resume_flag
        if self.resume_command:
            d["resume_command"] = self.resume_command
        if self.worktree:
            d["worktree"] = self.worktree
        return d
