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
        self.command = "claude"
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
        self._load_history()

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

        resume_id = self._agent_options.get("resume")
        if resume_id:
            self.resume_id = resume_id

        # Queue for receiving answers to AskUserQuestion from the UI
        self._question_answer_queue: asyncio.Queue[str] = asyncio.Queue()

        # Hook to intercept AskUserQuestion — emit to UI and wait for answer
        async def _ask_user_hook(tool_input, tool_use_id=None, **kwargs):
            question = tool_input.get("question", tool_input.get("text", ""))
            options_list = tool_input.get("options", tool_input.get("choices", []))

            # Emit question event to all subscribers
            self._emit_event({
                "type": "question",
                "question": question,
                "options": options_list,
                "tool_use_id": tool_use_id,
            })

            # Wait for the user's answer from the UI
            try:
                answer = await asyncio.wait_for(
                    self._question_answer_queue.get(), timeout=300
                )
            except asyncio.TimeoutError:
                answer = "No answer provided (timeout)"

            return {"content": [{"type": "text", "text": answer}]}

        try:
            hooks_config = {
                "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [_ask_user_hook]}]
            }
        except Exception:
            hooks_config = None

        options = ClaudeAgentOptions(
            cwd=self.cwd or ".",
            allowed_tools=self._agent_options.get("allowed_tools"),
            permission_mode=self._agent_options.get(
                "permission_mode", "bypassPermissions"
            ),
            system_prompt=self._agent_options.get("system_prompt"),
            max_turns=self._agent_options.get("max_turns"),
            model=self._agent_options.get("model"),
            resume=resume_id,
            continue_conversation=bool(resume_id),
            include_partial_messages=True,
            setting_sources=["user", "project"],
        )
        # Try to add hooks (SDK version may not support them)
        if hooks_config:
            try:
                options.hooks = hooks_config
            except Exception:
                pass

        try:
            async with ClaudeSDKClient(options=options) as client:
                self._client = client

                # Send initial prompt (skip if empty or just a command name)
                initial = self.prompt.strip()
                if initial and initial not in ("claude", "claude-agent", "Resume session"):
                    self._emit_event({
                        "type": "user_message",
                        "content": initial,
                    })
                    await client.query(initial)
                    await self._stream_response(client)

                # Wait for follow-up prompts
                while self.status == "running":
                    try:
                        item = await self._input_queue.get()
                        if isinstance(item, dict) and item.get("_shutdown"):
                            break
                        is_btw = False
                        if isinstance(item, dict):
                            text = item.get("text", "")
                            attachments = item.get("attachments")
                            is_btw = item.get("_btw", False)
                        else:
                            text = item
                            attachments = None
                        # Assign a turn ID to group query + response
                        self._turn_counter = getattr(self, '_turn_counter', 0) + 1
                        turn_id = f"turn-{self._turn_counter}"
                        self._current_turn_id = turn_id
                        self._current_turn_btw = is_btw
                        if is_btw:
                            self._broadcast_event({"type": "btw_start", "text": text})
                        # Save to history (skip for btw — ephemeral)
                        if not is_btw:
                            self._save_to_history({
                                "type": "user_message",
                                "content": text,
                                "turn_id": turn_id,
                                "timestamp": time.time(),
                            })
                        if attachments:
                            prompt_with_files = self._build_prompt_with_attachments(
                                text, attachments
                            )
                            await client.query(prompt_with_files)
                        else:
                            await client.query(text)
                        await self._stream_response(client)
                        if is_btw:
                            self._broadcast_event({"type": "btw_end"})
                    except asyncio.CancelledError:
                        # Interrupt — stay in the loop, wait for next prompt
                        continue
                    except Exception as turn_err:
                        # Per-turn error — don't kill the session
                        err_msg = str(turn_err)
                        # Ignore known interrupt-related errors
                        if "interrupt" in err_msg.lower() or "cancel" in err_msg.lower():
                            continue
                        self._emit_event({
                            "type": "error",
                            "error": err_msg,
                        })
                        # Stay in the loop — user can retry

        except Exception as e:
            # Fatal error (SDK connection failed, etc.)
            err_msg = str(e)
            if err_msg and "interrupt" not in err_msg.lower():
                self._emit_event({"type": "error", "error": err_msg})
        finally:
            self._client = None
            if self.status not in ("exited", "killed"):
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

        try:
            response_iter = client.receive_response()
        except Exception:
            return

        async for message in response_iter:
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
                # Persist metadata now that we have a resume ID —
                # if the server crashes, this survives for recovery.
                if self._on_exit:
                    try:
                        from be_conductor.utils.config import SESSIONS_DIR
                        import json as _json
                        path = SESSIONS_DIR / f"{self.id}.json"
                        path.write_text(_json.dumps(self.to_dict(), indent=2))
                    except Exception:
                        pass
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

    def _history_path(self):
        """Path to the persisted message history file."""
        from be_conductor.utils.config import SESSIONS_DIR
        return SESSIONS_DIR / f"{self.id}.history.json"

    def _load_history(self) -> None:
        """Load message history from disk (if exists)."""
        import json as _json
        path = self._history_path()
        if path.exists():
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    # Filter out stale session_end events from previous runs
                    data = [e for e in data if e.get("type") not in ("session_end",)]
                    self._message_history = data
                    for event in data:
                        self._append_console(event)
                    # Recover resume_id from history if not already set
                    if not self.resume_id:
                        for evt in reversed(data):
                            if evt.get("type") == "result" and evt.get("session_id"):
                                self.resume_id = evt["session_id"]
                                break
            except Exception:
                pass

    def _save_history(self) -> None:
        """Persist message history to disk."""
        import json as _json
        try:
            path = self._history_path()
            path.write_text(
                _json.dumps(self._message_history, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def delete_history(self) -> None:
        """Remove persisted history file."""
        try:
            self._history_path().unlink(missing_ok=True)
        except Exception:
            pass

    def _save_to_history(self, event: dict) -> None:
        """Save event to history + disk only (no broadcast, no console)."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        self._message_history.append(event)
        self._save_history()

    def _broadcast_event(self, event: dict) -> None:
        """Broadcast event to subscribers WITHOUT saving to history."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _emit_event(self, event: dict) -> None:
        """Broadcast a structured event and append to console buffer."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        # Tag with current turn ID for query/response grouping
        turn_id = getattr(self, '_current_turn_id', None)
        if turn_id and 'turn_id' not in event:
            event['turn_id'] = turn_id
            if getattr(self, '_current_turn_btw', False):
                event['btw'] = True
        self._message_history.append(event)
        self._save_history()
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
        btw: bool = False,
    ) -> None:
        """Enqueue a follow-up prompt, optionally with file attachments."""
        msg: dict | str
        if attachments or btw:
            msg = {"text": text}
            if attachments:
                msg["attachments"] = attachments
            if btw:
                msg["_btw"] = True
            self._input_queue.put_nowait(msg)
        else:
            self._input_queue.put_nowait(text)

    async def _send_btw(self, text: str) -> None:
        """Send a /btw query directly to Claude Code CLI, bypassing the queue.

        The /btw prefix tells Claude Code to handle this as an ephemeral
        side-channel query that doesn't get added to conversation history.
        """
        if not self._client:
            self._emit_event({"type": "btw_end", "error": "Not connected"})
            return
        self._btw_active = True
        self._btw_response_text = ""
        # Emit btw_start so frontend shows the panel
        for queue in list(self.subscribers):
            try:
                queue.put_nowait({"type": "btw_start", "text": text, "timestamp": time.time()})
            except asyncio.QueueFull:
                pass
        try:
            await self._client.query("/btw " + text)
        except Exception as e:
            self._btw_active = False
            for queue in list(self.subscribers):
                try:
                    queue.put_nowait({"type": "btw_end", "error": str(e), "timestamp": time.time()})
                except asyncio.QueueFull:
                    pass

    def send_input_bytes(self, data: bytes) -> None:
        self.send_input(data.decode("utf-8", errors="replace"))

    def answer_question(self, answer: str) -> None:
        """Provide an answer to a pending AskUserQuestion."""
        if hasattr(self, '_question_answer_queue'):
            self._question_answer_queue.put_nowait(answer)

    def set_mode(self, mode: str) -> None:
        """Change the agent permission mode at runtime.

        Valid modes: "default", "plan", "acceptEdits".
        """
        self._current_mode = mode
        if self._client is None:
            return
        try:
            self._client.set_permission_mode(mode)
        except AttributeError:
            pass
        except Exception:
            pass
        self._broadcast_settings()

    def set_effort(self, effort: str) -> None:
        """Change the agent effort level at runtime.

        Valid levels: "low", "medium", "high", "max".
        """
        self._current_effort = effort
        if self._client is None:
            return
        try:
            self._client.set_model(effort=effort)
        except (AttributeError, TypeError):
            pass
        except Exception:
            pass
        self._broadcast_settings()

    async def set_model_async(self, model: str) -> None:
        """Change the model at runtime."""
        self._current_model = model
        if self._client is None:
            return
        try:
            await self._client.set_model(model if model != 'default' else None)
        except Exception:
            pass
        self._broadcast_settings()

    async def get_models(self) -> list:
        """Get available models from the SDK."""
        if self._client is not None:
            try:
                info = await self._client.get_server_info()
                if info and 'models' in info:
                    return info['models']
            except Exception:
                pass
        # Fallback — common Claude models
        return [
            {"value": "default", "displayName": "Default (Opus 4.6)", "description": "Most capable, 1M context"},
            {"value": "sonnet", "displayName": "Sonnet 4.6", "description": "Best for everyday tasks"},
            {"value": "haiku", "displayName": "Haiku 4.5", "description": "Fastest for quick answers"},
        ]

    def _broadcast_settings(self) -> None:
        """Broadcast current mode/effort/model to all subscribers."""
        event = {
            "type": "settings",
            "mode": getattr(self, '_current_mode', 'default'),
            "effort": getattr(self, '_current_effort', 'high'),
            "model": getattr(self, '_current_model', 'default'),
        }
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def get_settings(self) -> dict:
        """Return current mode/effort/model for new subscribers."""
        return {
            "mode": getattr(self, '_current_mode', 'default'),
            "effort": getattr(self, '_current_effort', 'high'),
            "model": getattr(self, '_current_model', 'default'),
        }

    def _build_prompt_with_attachments(
        self,
        text: str,
        attachments: list[dict],
    ) -> str:
        """Build a text prompt with attachments saved to temp files.

        Images and binary files are saved to a temp directory so Claude
        can access them via the Read tool. Text files are inlined.
        """
        import base64
        import tempfile
        from pathlib import Path

        parts: list[str] = []
        for att in attachments:
            mime = att.get("type", "application/octet-stream")
            data = att.get("data", "")
            name = att.get("name", "file")
            if mime.startswith("image/"):
                # Save image to temp file so Claude can read it
                try:
                    raw = base64.b64decode(data)
                    tmp_dir = Path(tempfile.gettempdir()) / "be-conductor-uploads"
                    tmp_dir.mkdir(exist_ok=True)
                    tmp_path = tmp_dir / name
                    tmp_path.write_bytes(raw)
                    parts.append(
                        f"I've attached an image. It's saved at: {tmp_path}\n"
                        f"Please use the Read tool to view it."
                    )
                except Exception:
                    parts.append(f"[Attached image: {name} — failed to save]")
            else:
                try:
                    decoded = base64.b64decode(data).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    decoded = "(binary file)"
                parts.append(f"[Attached file: {name}]\n{decoded}")
        if text:
            parts.append(text)
        return "\n\n".join(parts)

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
        """Interrupt the current query, or stop the session if graceful-stopping."""
        if self.status == "stopping":
            # Graceful stop: interrupt any running query, then signal the
            # agent loop to exit by changing status and unblocking the queue.
            if self._client:
                asyncio.ensure_future(self._do_interrupt())
            asyncio.ensure_future(self._graceful_shutdown())
            return
        if self._client:
            asyncio.ensure_future(self._do_interrupt())
        # Don't cancel _run_task — the SDK's interrupt will stop the
        # current query and the loop will wait for the next prompt.

    async def _do_interrupt(self) -> None:
        if self._client:
            try:
                await self._client.interrupt()
            except Exception:
                pass

    async def _graceful_shutdown(self) -> None:
        """Signal the agent loop to exit for graceful stop."""
        self._was_graceful = True
        # Unblock _input_queue.get() so the loop can check status and exit
        try:
            self._input_queue.put_nowait({"text": "", "_shutdown": True})
        except Exception:
            pass
        # Give the loop a moment to exit cleanly, then force-cancel.
        # The finally block in _agent_loop handles status and _on_exit.
        await asyncio.sleep(3)
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

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
