"""Agent SDK session — structured events instead of raw PTY output.

Wraps the Claude Agent SDK (or future agent SDKs) and streams typed
JSON events to subscribers.  A parallel console buffer accumulates
ANSI-formatted text so get_buffer() / get_screen_snapshot() still
work for backwards-compatible consumers (CLI, console-mode toggle).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Set

BUFFER_MAX_BYTES = 1_000_000

# Reuse the ANSI-stripping regex from session.py
_ANSI_RE = re.compile(
    r'\x1b'
    r'(?:'
    r'\[[\x20-\x3f]*[a-zA-Z@-~]'
    r'|\][^\x07]*\x07'
    r'|[()][AB012]'
    r'|[>=<]'
    r'|#[0-9]'
    r'|.'
    r')'
)


def _format_event_ansi(event: dict) -> str | None:
    """Format a structured agent event as ANSI-colored terminal text."""
    etype = event.get("type")

    if etype == "user_message":
        content = event.get("content", "")
        return f"\r\n\033[1;36m>>> User\033[0m\r\n{content}\r\n"

    elif etype == "assistant_message":
        parts = []
        for block in event.get("content", []):
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                parts.append("\033[90m[thinking...]\033[0m")
            elif btype == "tool_use":
                tool = block.get("tool", "")
                inp = block.get("input", {})
                summary = str(inp)[:200]
                parts.append(f"\033[1;33m[{tool}]\033[0m {summary}")
            elif btype == "tool_result":
                content = block.get("content", "")
                if block.get("is_error"):
                    parts.append(f"\033[1;31m[error]\033[0m {str(content)[:200]}")
                else:
                    parts.append(f"\033[1;32m[result]\033[0m {str(content)[:200]}")
        if parts:
            return "\r\n\033[1;32m<<< Claude\033[0m\r\n" + "\r\n".join(parts) + "\r\n"

    elif etype == "result":
        result = event.get("result", "")
        cost = event.get("total_cost_usd")
        suffix = f" (${cost:.4f})" if cost else ""
        return f"\r\n\033[1;35m[done]\033[0m {result}{suffix}\r\n"

    elif etype == "error":
        error = event.get("error", "")
        return f"\r\n\033[1;31m[error] {error}\033[0m\r\n"

    elif etype == "session_end":
        code = event.get("exit_code", 0)
        return f"\r\n[session ended (exit {code})]\r\n"

    return None


class AgentSession:
    """A session backed by the Claude Agent SDK (or future agent SDKs)."""

    session_type: str = "agent"

    def __init__(
        self,
        name: str,
        prompt: str,
        session_id: str | None = None,
        cwd: str | None = None,
        on_exit=None,
        env: dict | None = None,
        worktree: dict | None = None,
        notifier=None,
        agent_options: dict | None = None,
    ):
        self.id = session_id or name
        self.name = name
        self.command = "claude-agent"
        self.prompt = prompt
        self.cwd = cwd
        self.worktree = worktree
        self.status = "starting"
        self.pid: int | None = None
        self.start_time: float | None = None
        self.created_at: str | None = None
        self.exit_code: int | None = None
        self.resume_id: str | None = None
        self.rows: int = 24
        self.cols: int = 80
        self.subscribers: Set[asyncio.Queue] = set()
        self._on_exit = on_exit
        self._notifier = notifier
        self._agent_options = agent_options or {}

        # Console buffer (ANSI text for get_buffer / console mode)
        self._console_buffer = bytearray()

        # Structured message history (for replay to new subscribers)
        self._message_history: list[dict] = []

        # SDK state
        self._client: Any = None
        self._run_task: asyncio.Task | None = None
        self._input_queue: asyncio.Queue[dict | str] = asyncio.Queue()

        # Client tracking
        self._attached_sources: dict[str, str] = {}
        self.resize_source: str | None = None
        self.resize_owner_id: str | None = None
        self.browser_resize_owner_id: str | None = None
        self.cli_attach_count: int = 0

    async def start(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols
        self.start_time = time.time()
        self.created_at = datetime.fromtimestamp(
            self.start_time, tz=timezone.utc
        ).isoformat()
        self.status = "running"
        self._run_task = asyncio.create_task(self._agent_loop())

    async def _agent_loop(self) -> None:
        """Main loop: send prompt, stream responses, accept follow-ups."""
        try:
            from claude_agent_sdk import (
                ClaudeSDKClient,
                ClaudeAgentOptions,
                AssistantMessage,
                ResultMessage,
                SystemMessage,
                StreamEvent,
                RateLimitEvent,
            )
        except ImportError:
            self._emit_event({
                "type": "error",
                "error": (
                    "claude-agent-sdk is not installed. "
                    "Install with: pip install claude-agent-sdk"
                ),
            })
            self.status = "exited"
            self.exit_code = 1
            self._broadcast_close()
            if self._on_exit:
                await self._on_exit(self.id)
            return

        options = ClaudeAgentOptions(
            cwd=self.cwd or ".",
            allowed_tools=self._agent_options.get("allowed_tools"),
            permission_mode=self._agent_options.get(
                "permission_mode", "default"
            ),
            system_prompt=self._agent_options.get("system_prompt"),
            max_turns=self._agent_options.get("max_turns"),
            model=self._agent_options.get("model"),
            resume=self._agent_options.get("resume"),
            include_partial_messages=True,
        )

        try:
            async with ClaudeSDKClient(options=options) as client:
                self._client = client

                # Send initial prompt
                self._emit_event({
                    "type": "user_message",
                    "content": self.prompt,
                })
                await client.query(self.prompt)
                await self._stream_response(client)

                # Wait for follow-up prompts
                while self.status == "running":
                    try:
                        item = await self._input_queue.get()
                        if isinstance(item, dict):
                            text = item.get("text", "")
                            attachments = item.get("attachments")
                        else:
                            text = item
                            attachments = None
                        if attachments:
                            # Build content blocks for the SDK
                            content_blocks = self._build_prompt_blocks(
                                text, attachments
                            )
                            await client.query(content_blocks)
                        else:
                            await client.query(text)
                        await self._stream_response(client)
                    except asyncio.CancelledError:
                        break

        except Exception as e:
            self._emit_event({"type": "error", "error": str(e)})
        finally:
            self._client = None
            if self.status == "running":
                self.status = "exited"
                self.exit_code = 0
            self._emit_event({
                "type": "session_end",
                "exit_code": self.exit_code,
            })
            self._broadcast_close()
            if self._on_exit:
                await self._on_exit(self.id)

    async def _stream_response(self, client: Any) -> None:
        """Stream all messages from one query() call."""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            RateLimitEvent,
        )

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                self._emit_event(self._format_assistant(message))
            elif isinstance(message, ResultMessage):
                self._emit_event({
                    "type": "result",
                    "result": message.result,
                    "is_error": message.is_error,
                    "stop_reason": getattr(message, "stop_reason", None),
                    "duration_ms": message.duration_ms,
                    "num_turns": message.num_turns,
                    "total_cost_usd": getattr(message, "total_cost_usd", None),
                    "usage": getattr(message, "usage", None),
                    "session_id": message.session_id,
                })
                self.resume_id = message.session_id
            elif isinstance(message, SystemMessage):
                self._emit_event({
                    "type": "system",
                    "subtype": message.subtype,
                    "data": message.data,
                })
                if message.subtype == "init":
                    sid = message.data.get("session_id")
                    if sid:
                        self.resume_id = sid
            elif isinstance(message, RateLimitEvent):
                rli = getattr(message, "rate_limit_info", None)
                self._emit_event({
                    "type": "rate_limit",
                    "info": str(rli) if rli else None,
                })

    @staticmethod
    def _format_assistant(message: Any) -> dict:
        """Convert an AssistantMessage to our wire format."""
        from claude_agent_sdk import (
            TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )

        blocks: list[dict] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                })
            elif isinstance(block, ToolUseBlock):
                blocks.append({
                    "type": "tool_use",
                    "tool": block.name,
                    "tool_use_id": block.id,
                    "input": block.input,
                })
            elif isinstance(block, ToolResultBlock):
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": (
                        str(block.content) if block.content else None
                    ),
                    "is_error": getattr(block, "is_error", False),
                })

        return {
            "type": "assistant_message",
            "content": blocks,
            "model": getattr(message, "model", None),
            "usage": getattr(message, "usage", None),
        }

    # ------------------------------------------------------------------
    # Event broadcast
    # ------------------------------------------------------------------

    @staticmethod
    def _json_safe(obj: Any) -> Any:
        """Recursively convert non-serializable objects to strings."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {k: AgentSession._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [AgentSession._json_safe(v) for v in obj]
        return str(obj)

    def _emit_event(self, event: dict) -> None:
        """Broadcast a structured event and append to console buffer."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        self._message_history.append(event)
        self._append_console(event)

        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Coalesce: drain and re-enqueue recent items
                merged: list[dict] = []
                try:
                    while not queue.empty():
                        merged.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    pass
                merged.append(event)
                for item in merged[-100:]:
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        break

    def _append_console(self, event: dict) -> None:
        text = _format_event_ansi(event)
        if text:
            data = text.encode("utf-8", errors="replace")
            self._console_buffer.extend(data)
            if len(self._console_buffer) > BUFFER_MAX_BYTES:
                excess = len(self._console_buffer) - BUFFER_MAX_BYTES
                del self._console_buffer[:excess]

    def _broadcast_close(self) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # Public interface (matches SessionProtocol)
    # ------------------------------------------------------------------

    def send_input(
        self,
        text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        """Enqueue a follow-up prompt, optionally with file attachments."""
        if attachments:
            self._input_queue.put_nowait({
                "text": text,
                "attachments": attachments,
            })
        else:
            self._input_queue.put_nowait(text)

    def send_input_bytes(self, data: bytes) -> None:
        self.send_input(data.decode("utf-8", errors="replace"))

    def set_mode(self, mode: str) -> None:
        """Change the agent permission mode at runtime.

        Valid modes: "default", "plan", "acceptEdits".
        """
        if self._client is None:
            return
        try:
            self._client.set_permission_mode(mode)
        except AttributeError:
            pass
        except Exception:
            pass

    def set_effort(self, effort: str) -> None:
        """Change the agent effort level at runtime.

        Valid levels: "low", "medium", "high".
        """
        if self._client is None:
            return
        try:
            self._client.set_model(effort=effort)
        except (AttributeError, TypeError):
            pass
        except Exception:
            pass

    @staticmethod
    def _build_prompt_blocks(
        text: str,
        attachments: list[dict],
    ) -> list[dict]:
        """Build SDK-compatible content blocks from text + attachments."""
        import base64

        blocks: list[dict] = []
        for att in attachments:
            mime = att.get("type", "application/octet-stream")
            data = att.get("data", "")
            if mime.startswith("image/"):
                # Validate base64 by attempting decode
                try:
                    base64.b64decode(data)
                except Exception:
                    continue
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": data,
                    },
                })
            else:
                # Non-image files: send as text with filename context
                try:
                    decoded = base64.b64decode(data).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    decoded = "(binary file)"
                name = att.get("name", "file")
                blocks.append({
                    "type": "text",
                    "text": f"[Attached file: {name}]\n{decoded}",
                })
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def get_message_history(self) -> list[dict]:
        """Return structured message history for replay."""
        return list(self._message_history)

    def get_buffer(self) -> bytes:
        return bytes(self._console_buffer)

    def get_buffer_text(self, max_lines: int = 500) -> str:
        raw = self._console_buffer.decode("utf-8", errors="replace")
        clean = _ANSI_RE.sub("", raw)
        lines = clean.splitlines()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)

    def get_screen_snapshot(self, clean: bool = False) -> bytes:
        return bytes(self._console_buffer)

    def resize(self, rows: int, cols: int, source: str | None = None,
               client_id: str | None = None) -> None:
        self.rows = rows
        self.cols = cols

    def interrupt(self, timeout: float = 30.0) -> None:
        self.status = "stopping"
        if self._client:
            asyncio.ensure_future(self._do_interrupt())
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    async def _do_interrupt(self) -> None:
        if self._client:
            try:
                await self._client.interrupt()
            except Exception:
                pass

    async def kill(self) -> None:
        self.status = "killed"
        self.exit_code = -9
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
        self._broadcast_close()

    async def cleanup(self) -> None:
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Client tracking (simplified — no resize authority needed)
    # ------------------------------------------------------------------

    def cli_connected(self, client_id: str) -> None:
        self.cli_attach_count += 1
        if client_id:
            self._attached_sources[client_id] = "cli"

    def cli_disconnected(self, client_id: str) -> None:
        self.cli_attach_count = max(0, self.cli_attach_count - 1)
        if client_id:
            self._attached_sources.pop(client_id, None)

    def browser_connected(self, client_id: str, source: str = "browser") -> None:
        if client_id:
            self._attached_sources[client_id] = source

    def browser_disconnected(self, client_id: str) -> None:
        if client_id:
            self._attached_sources.pop(client_id, None)

    @property
    def attached_clients(self) -> list[dict]:
        return [
            {"client_id": cid, "source": src}
            for cid, src in self._attached_sources.items()
        ]

    @property
    def live_cwd(self) -> str | None:
        return self.cwd

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "session_type": self.session_type,
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
            "message_count": len(self._message_history),
        }
        if self.resume_id:
            d["resume_id"] = self.resume_id
        if self.worktree:
            d["worktree"] = self.worktree
        return d
