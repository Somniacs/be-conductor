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

"""Tail a Claude Code JSONL file and stream ANSI-formatted text for observation."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

log = logging.getLogger(__name__)

# ANSI color codes for formatting
_CYAN_BOLD = "\033[1;36m"
_GREEN_BOLD = "\033[1;32m"
_YELLOW_BOLD = "\033[1;33m"
_DIM = "\033[90m"
_RESET = "\033[0m"

# Max records to include in initial history buffer
_MAX_HISTORY_RECORDS = 200


class SessionObserver:
    """Tails a JSONL session file and streams ANSI-formatted text to subscribers."""

    def __init__(self, jsonl_path: Path):
        self.path = jsonl_path
        self._buffer = bytearray()
        self._subscribers: Set[asyncio.Queue] = set()
        self._tail_task: asyncio.Task | None = None
        self._file_pos: int = 0
        self._running = False

    async def start(self):
        """Read entire file, format all records, start tail loop."""
        self._running = True
        loop = asyncio.get_event_loop()
        # Read and format existing content in executor (blocking I/O)
        initial = await loop.run_in_executor(None, self._read_initial)
        self._buffer.extend(initial)
        # Start tail loop
        self._tail_task = asyncio.create_task(self._tail_loop())

    def _read_initial(self) -> bytes:
        """Read the entire file and format all records (blocking)."""
        chunks = []
        record_count = 0
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    formatted = self._format_record(record)
                    if formatted:
                        record_count += 1
                        chunks.append(formatted)
                # Record file position for tailing
                self._file_pos = f.tell()
        except FileNotFoundError:
            return b"[Session file not found]\r\n"
        except OSError as e:
            return f"[Error reading session: {e}]\r\n".encode()

        # Limit to last N records for large files
        if len(chunks) > _MAX_HISTORY_RECORDS:
            chunks = chunks[-_MAX_HISTORY_RECORDS:]
            prefix = f"{_DIM}[... showing last {_MAX_HISTORY_RECORDS} of {record_count} records ...]{_RESET}\r\n\r\n"
            chunks.insert(0, prefix)

        return "".join(chunks).encode("utf-8", errors="replace")

    async def _tail_loop(self):
        """Poll the file for growth and broadcast new records."""
        while self._running:
            try:
                await asyncio.sleep(0.5)
                if not self._running:
                    break

                loop = asyncio.get_event_loop()
                new_data = await loop.run_in_executor(None, self._read_new)
                if new_data:
                    self._buffer.extend(new_data)
                    # Trim buffer if too large (keep last 512KB)
                    if len(self._buffer) > 512 * 1024:
                        excess = len(self._buffer) - 512 * 1024
                        del self._buffer[:excess]
                    self._broadcast(new_data)
            except asyncio.CancelledError:
                break
            except FileNotFoundError:
                msg = b"\r\n[Session file removed]\r\n"
                self._broadcast(msg)
                break
            except Exception:
                log.debug("Observer tail error for %s", self.path, exc_info=True)
                await asyncio.sleep(2)

    def _read_new(self) -> bytes | None:
        """Read new lines since last position (blocking)."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            raise FileNotFoundError(self.path)

        if size <= self._file_pos:
            return None

        chunks = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._file_pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                formatted = self._format_record(record)
                if formatted:
                    chunks.append(formatted)
            self._file_pos = f.tell()

        if not chunks:
            return None
        return "".join(chunks).encode("utf-8", errors="replace")

    @staticmethod
    def _format_record(record: dict) -> str | None:
        """Convert a JSONL record to ANSI-colored text for xterm.js display."""
        rtype = record.get("type", "")

        # Skip noise
        if rtype in ("file-history-snapshot", "progress"):
            return None

        # Timestamp prefix
        timestamp_str = record.get("timestamp", "")
        time_prefix = ""
        if timestamp_str:
            try:
                dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                local_dt = dt.astimezone()
                time_prefix = f"{_DIM}[{local_dt.strftime('%H:%M:%S')}]{_RESET} "
            except (ValueError, OSError):
                pass

        if rtype == "user":
            message = record.get("message", {})
            content = message.get("content", "")
            if isinstance(content, list):
                # Extract text blocks
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                content = "\n".join(texts)
            if isinstance(content, str) and content.strip():
                # Truncate very long user messages
                display = content.strip()
                if len(display) > 500:
                    display = display[:500] + "..."
                return f"\r\n{time_prefix}{_CYAN_BOLD}>>> User{_RESET}\r\n{_escape_for_terminal(display)}\r\n"

        elif rtype == "assistant":
            message = record.get("message", {})
            content = message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                return None

            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        parts.append(f"{_escape_for_terminal(text)}")
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    # Show a compact summary of the tool call
                    summary = _tool_summary(tool_name, tool_input)
                    parts.append(f"{_YELLOW_BOLD}[Tool: {tool_name}]{_RESET} {summary}")

            if parts:
                header = f"\r\n{time_prefix}{_GREEN_BOLD}<<< Assistant{_RESET}\r\n"
                return header + "\r\n".join(parts) + "\r\n"

        elif rtype == "tool_result":
            # Show brief tool results
            return None

        return None

    def _broadcast(self, data: bytes):
        """Send data to all subscribers."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._subscribers.discard(queue)

    def get_buffer(self) -> bytes:
        """Return current buffer for initial WebSocket replay."""
        return bytes(self._buffer)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stop(self):
        """Stop the tail loop and clean up."""
        self._running = False
        if self._tail_task:
            self._tail_task.cancel()
            try:
                await self._tail_task
            except asyncio.CancelledError:
                pass
            self._tail_task = None


def _escape_for_terminal(text: str) -> str:
    """Escape text for terminal display (convert newlines to \\r\\n)."""
    return text.replace("\n", "\r\n")


def _tool_summary(name: str, input_data: dict) -> str:
    """Generate a compact one-line summary of a tool call."""
    if name == "Read":
        return input_data.get("file_path", "")
    elif name == "Write":
        return input_data.get("file_path", "")
    elif name == "Edit":
        return input_data.get("file_path", "")
    elif name == "Bash":
        cmd = input_data.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return cmd
    elif name == "Glob":
        return input_data.get("pattern", "")
    elif name == "Grep":
        return input_data.get("pattern", "")
    elif name in ("WebFetch", "WebSearch"):
        return input_data.get("url", "") or input_data.get("query", "")
    elif name == "Agent":
        return input_data.get("description", "")
    else:
        # Generic: show first string value
        for v in input_data.values():
            if isinstance(v, str) and v:
                s = v
                if len(s) > 60:
                    s = s[:57] + "..."
                return s
        return ""
