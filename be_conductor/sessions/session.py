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

import pyte

from be_conductor.proxy.pty_wrapper import PTYProcess
from be_conductor.utils import config as cfg
from be_conductor.utils.config import UPLOADS_DIR

logger = logging.getLogger(__name__)

# pyte encodes private mode numbers by left-shifting by 5.
_PYTE_ALT_MODE = 1049 << 5  # alternate screen buffer (DECALTBUF)

# Map pyte named colors → ANSI 3-bit colour indices (0-7).
_PYTE_COLOR_NAMES = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
}


def _color_sgr(color: str, is_bg: bool) -> str:
    """Convert a pyte colour value to an SGR parameter fragment.

    Returns an empty string for the default colour.
    """
    if color == "default" or not color:
        return ""
    base = 40 if is_bg else 30
    idx = _PYTE_COLOR_NAMES.get(color)
    if idx is not None:
        return str(base + idx)
    # pyte stores 256/true-colour values as 6-char hex strings ("ff0000").
    if len(color) == 6:
        try:
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            return f"{base + 8};2;{r};{g};{b}"
        except ValueError:
            pass
    return ""


def _render_pyte_screen(screen: pyte.Screen, in_alt: bool) -> bytes:
    """Render the current pyte screen state as ANSI escape sequences.

    Produces a compact byte string that, when written to a terminal,
    reproduces the screen contents including colours and attributes.
    """
    buf = bytearray()

    if in_alt:
        buf.extend(b"\x1b[?1049h")   # enter alternate screen

    buf.extend(b"\x1b[?25l")         # hide cursor during draw
    buf.extend(b"\x1b[2J\x1b[H")     # clear screen + cursor home

    default = screen.default_char
    prev = default

    for y in range(screen.lines):
        row = screen.buffer[y]

        # Find rightmost non-default cell to skip trailing blanks.
        last = -1
        for x in range(screen.columns - 1, -1, -1):
            if row[x] != default:
                last = x
                break
        if last < 0:
            continue  # entirely empty line

        buf.extend(f"\x1b[{y + 1};1H".encode())  # move to line start

        for x in range(last + 1):
            ch = row[x]

            # Emit SGR only when attributes change.
            if (ch.fg != prev.fg or ch.bg != prev.bg or
                    ch.bold != prev.bold or ch.italics != prev.italics or
                    ch.underscore != prev.underscore or
                    ch.reverse != prev.reverse or
                    ch.strikethrough != prev.strikethrough or
                    ch.blink != prev.blink):
                parts = ["0"]  # reset, then re-apply
                if ch.bold:
                    parts.append("1")
                if ch.italics:
                    parts.append("3")
                if ch.underscore:
                    parts.append("4")
                if ch.blink:
                    parts.append("5")
                if ch.reverse:
                    parts.append("7")
                if ch.strikethrough:
                    parts.append("9")
                fg = _color_sgr(ch.fg, False)
                if fg:
                    parts.append(fg)
                bg = _color_sgr(ch.bg, True)
                if bg:
                    parts.append(bg)
                buf.extend(f"\x1b[{';'.join(parts)}m".encode())
                prev = ch

            buf.extend(ch.data.encode("utf-8") if ch.data else b" ")

        # Reset at end of each rendered line so trailing state doesn't leak.
        buf.extend(b"\x1b[0m")
        prev = default

    # Restore scroll region if the application set one.
    if screen.margins is not None:
        buf.extend(f"\x1b[{screen.margins.top + 1};{screen.margins.bottom + 1}r".encode())

    # Restore non-default terminal modes (DECTCEM / cursor visibility
    # is handled separately below).
    _default_modes = {pyte.modes.DECAWM, pyte.modes.DECTCEM}
    _mode_seqs = {
        pyte.modes.IRM: b"\x1b[4h",         # insert mode
        pyte.modes.LNM: b"\x1b[20h",        # line-feed / new-line mode
        pyte.modes.DECSCNM: b"\x1b[?5h",    # reverse video
        pyte.modes.DECOM: b"\x1b[?6h",      # origin mode
    }
    # Enable modes that are active but not default.
    for mode, seq in _mode_seqs.items():
        if mode in screen.mode:
            buf.extend(seq)
    # Disable auto-wrap if it was turned off (it's on by default).
    if pyte.modes.DECAWM not in screen.mode:
        buf.extend(b"\x1b[?7l")

    # Final cursor position & visibility.
    buf.extend(f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}H".encode())
    if not screen.cursor.hidden:
        buf.extend(b"\x1b[?25h")

    return bytes(buf)

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
        self._attached_sources: dict[str, str] = {}  # client_id → source (cli/browser/vscode/jetbrains)
        self._monitor_task: asyncio.Task | None = None
        self._on_exit = on_exit
        self._reader_thread: threading.Thread | None = None
        self._queue_overflow_warned: bool = False
        self._notifier = notifier  # SessionNotifier instance (optional)

        # Virtual terminal for screen snapshots — allows new clients to
        # receive a compact, accurate screen state instead of replaying the
        # full raw output buffer (which can cause scroll jumping and TUI
        # rendering issues in terminal emulators).
        self._pyte_screen = pyte.Screen(80, 24)
        self._pyte_stream = pyte.Stream(self._pyte_screen)

    async def start(self, rows: int = 24, cols: int = 80):
        self.pty.spawn(rows=rows, cols=cols)
        self._pyte_screen.resize(rows, cols)
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
            # Drain ALL available data from the PTY in one call so that
            # full-screen redraws (clear + home + content) are broadcast
            # as a single chunk — matching how a direct terminal connection
            # delivers data.  The fd is non-blocking, so os.read raises
            # BlockingIOError when the buffer is empty.
            chunks = []
            try:
                while True:
                    chunk = os.read(self.pty.master_fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except BlockingIOError:
                pass
            if chunks:
                data = b"".join(chunks) if len(chunks) > 1 else chunks[0]
                self._append_buffer(data)
                self._broadcast(data)
        except OSError:
            # EIO means the slave side closed (process exited).
            # Unregister immediately to avoid a tight spin in the event loop.
            try:
                asyncio.get_event_loop().remove_reader(self.pty.master_fd)
            except Exception:
                pass
        except Exception:
            # Never let an unexpected error in _append_buffer / _broadcast
            # propagate into the event loop — that would crash the server.
            logger.exception("Unexpected error in _on_readable")

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
            except RuntimeError:
                # Event loop closed during shutdown — stop cleanly.
                break
            except Exception:
                break

    # -- Buffer & broadcast ------------------------------------------------

    def _append_buffer(self, data: bytes):
        self.buffer.extend(data)
        if len(self.buffer) > cfg.BUFFER_MAX_BYTES:
            excess = len(self.buffer) - cfg.BUFFER_MAX_BYTES
            # Advance past any UTF-8 continuation bytes (10xxxxxx) at the
            # cut point so we don't orphan the tail of a multi-byte char.
            while excess < len(self.buffer) and (self.buffer[excess] & 0xC0) == 0x80:
                excess += 1
            del self.buffer[:excess]
        # Feed the virtual terminal so screen snapshots stay current.
        try:
            self._pyte_stream.feed(data.decode("utf-8", errors="replace"))
        except Exception:
            pass
        # Feed the notifier so it can detect when the agent needs attention.
        if self._notifier:
            try:
                self._notifier.on_output(data, self.buffer)
            except Exception:
                pass

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

    def get_buffer_text(self, max_lines: int = 500) -> str:
        """Return the buffer as ANSI-stripped plaintext.

        Decodes the raw byte buffer, removes all escape sequences, and
        returns the last *max_lines* lines.  Useful for extracting
        human-readable context from a running session.
        """
        raw = bytes(self.buffer).decode("utf-8", errors="replace")
        clean = _ANSI_RE.sub("", raw)
        lines = clean.splitlines()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)

    def get_screen_snapshot(self, clean: bool = False) -> bytes:
        """Return a compact representation of the current terminal state.

        When *clean* is True (CLI clients) or the session is in
        alternate-screen mode, returns a pyte-rendered snapshot — a
        compact, accurate screen state without historical cursor
        movements that can cause scroll-jumping.

        Otherwise returns the raw buffer so scrollback history is
        preserved (dashboard).
        """
        in_alt = _PYTE_ALT_MODE in self._pyte_screen.mode
        if in_alt or clean:
            return _render_pyte_screen(self._pyte_screen, in_alt=in_alt)
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
        try:
            self._pyte_screen.resize(rows, cols)
        except Exception:
            pass

    def cli_connected(self, client_id: str):
        """Track a CLI WebSocket connection."""
        self.cli_attach_count += 1
        if client_id:
            self._attached_sources[client_id] = "cli"
        if not self.resize_owner_id:
            self.resize_owner_id = client_id

    def cli_disconnected(self, client_id: str):
        """Track a CLI WebSocket disconnection."""
        self.cli_attach_count = max(0, self.cli_attach_count - 1)
        if client_id:
            self._attached_sources.pop(client_id, None)
        if client_id == self.resize_owner_id:
            self.resize_owner_id = None  # Owner left — next CLI resize will claim
            if self.cli_attach_count == 0 and not self.browser_resize_owner_id:
                self.resize_source = None  # No CLI or browser owner → fresh start

    def browser_connected(self, client_id: str, source: str = "browser"):
        """Track a browser/IDE WebSocket connection."""
        if client_id:
            self._attached_sources[client_id] = source
        if not self.browser_resize_owner_id:
            self.browser_resize_owner_id = client_id

    def browser_disconnected(self, client_id: str):
        """Track a browser/IDE WebSocket disconnection."""
        if client_id:
            self._attached_sources.pop(client_id, None)
        if client_id == self.browser_resize_owner_id:
            self.browser_resize_owner_id = None
            if self.cli_attach_count == 0:
                self.resize_source = None

    @property
    def attached_clients(self) -> list[dict]:
        """Return a list of currently attached clients with their source."""
        return [{"client_id": cid, "source": src}
                for cid, src in self._attached_sources.items()]

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
            try:
                self._notifier.cancel()
            except Exception:
                pass

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

    @property
    def live_cwd(self) -> str | None:
        """Read the process's current working directory from /proc (Linux).

        Falls back to the initial cwd if unavailable.
        """
        if self.pid and not _IS_WIN:
            try:
                return os.readlink(f"/proc/{self.pid}/cwd")
            except OSError:
                pass
        return self.cwd

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
            "cwd": self.live_cwd,
            "rows": self.rows,
            "cols": self.cols,
            "resize_source": self.resize_source,
            "resize_owner": self.resize_owner_id or self.browser_resize_owner_id,
            "cli_attach_count": self.cli_attach_count,
            "attached_clients": self.attached_clients,
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
