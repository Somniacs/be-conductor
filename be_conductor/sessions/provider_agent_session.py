"""ProviderAgentSession — orchestrator for AgentProvider-backed sessions.

This is the be-conductor session class for any agent that satisfies the
`AgentProvider` protocol. It owns:

  - The subscriber set (WebSocket clients)
  - Structured message history (JSONL on disk + in-memory list)
  - The console buffer (ANSI text for /buffer endpoints)
  - The send queue (mid-turn injection, accept-while-busy)
  - The wire-protocol event broadcast

…and delegates all SDK-shaped work (start, send, events, interrupt,
stop) to the `AgentProvider` it wraps.

This class is **separate from** the existing `AgentSession`
([be_conductor/sessions/agent_session.py]), which remains the only
path used for Claude sessions. ProviderAgentSession is dispatched only
when the run-session route is given an explicit `provider="opencode"`
(or another non-Claude provider in the future). Existing Claude
sessions, the dashboard, the JetBrains/VSCode plugins, and any API
consumer that doesn't pass `provider` see no change.

For v1, the orchestrator is intentionally a leaner subset of
`AgentSession`'s feature surface — see docs/planned/agent-abstraction.md
"Implementation phasing" for what's deferred to later phases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Set

from be_conductor.sessions.providers.base import AgentEvent, AgentProvider

log = logging.getLogger(__name__)

BUFFER_MAX_BYTES = 1_000_000

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
    """Format a structured event as ANSI-coloured text for the console
    buffer fallback. Mirrors the AgentSession version but kept simple."""
    etype = event.get("type")
    if etype == "user_message":
        return f"\r\n\033[1;36m>>> User\033[0m\r\n{event.get('content','')}\r\n"
    if etype == "assistant_message":
        parts = []
        for block in event.get("content", []) or []:
            bt = block.get("type") if isinstance(block, dict) else None
            if bt == "text":
                parts.append(block.get("text", ""))
            elif bt == "thinking":
                parts.append(f"\033[2;3m[thinking]\033[0m {block.get('thinking','')}")
        if parts:
            return "\r\n\033[1;32m<<< Assistant\033[0m\r\n" + "\r\n".join(parts) + "\r\n"
    if etype == "turn_end":
        cost = event.get("total_cost_usd")
        suffix = f" (${cost:.4f})" if cost else ""
        return f"\r\n\033[1;35m[done]\033[0m{suffix}\r\n"
    if etype == "error":
        return f"\r\n\033[1;31m[error] {event.get('error','')}\033[0m\r\n"
    if etype == "session_end":
        return f"\r\n[session ended (exit {event.get('exit_code', 0)})]\r\n"
    return None


class ProviderAgentSession:
    """Session wrapper for an `AgentProvider`-backed coding agent."""

    session_type: str = "agent"

    def __init__(
        self,
        name: str,
        prompt: str,
        provider: AgentProvider,
        session_id: str | None = None,
        cwd: str | None = None,
        on_exit=None,
        worktree: dict | None = None,
        notifier=None,
        agent_options: dict | None = None,
    ):
        self.id = session_id or name
        self.name = name
        self._provider = provider
        # `command` is a label shown in the dashboard. Use the provider
        # name for clarity ("opencode" vs "claude").
        self.command = provider.name
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

        # Console buffer (ANSI fallback for /buffer endpoints).
        self._console_buffer = bytearray()
        # Structured history (replayed to new subscribers).
        self._message_history: list[dict] = []
        self._history_dirty = False
        self._history_saver_task: asyncio.Task | None = None
        self._load_history()

        # Tasks
        self._run_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._input_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._processing = False
        # Most recent permission request id (for answer_question routing).
        self._last_permission_request_id: str = ""

        # Turn-id tagging
        self._turn_prefix = uuid.uuid4().hex[:6]
        self._turn_counter = 0
        self._current_turn_id: str | None = None

        # Client tracking (mirrors AgentSession surface)
        self._attached_sources: dict[str, str] = {}
        self.resize_source: str | None = None
        self.resize_owner_id: str | None = None
        self.browser_resize_owner_id: str | None = None
        self.cli_attach_count: int = 0

    # ------------------------------------------------------------------
    # SessionProtocol — public lifecycle
    # ------------------------------------------------------------------

    async def start(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols
        self.start_time = time.time()
        self.created_at = datetime.fromtimestamp(
            self.start_time, tz=timezone.utc,
        ).isoformat()
        self.status = "running"
        self._run_task = asyncio.create_task(self._main_loop())

    def send_input(
        self,
        text: str,
        attachments: list[dict] | None = None,
        btw: bool = False,
    ) -> None:
        """Public entry — enqueues a user prompt for processing.

        The ``btw`` flag is be-conductor's BTW side-channel marker.
        Accepted here for signature compatibility with
        ``AgentSession.send_input``. The OpenCode adapter doesn't
        currently implement BTW, so the flag is silently ignored — but
        accepting the keyword is required: without it, the WebSocket
        prompt route's TypeError fallback was treating the entire
        JSON envelope as the prompt text and rendering the raw JSON
        as a user bubble in the UI.
        """
        if self.status != "running":
            return
        try:
            self._input_queue.put_nowait({
                "text": text,
                "attachments": attachments or [],
                "btw": bool(btw),
            })
        except asyncio.QueueFull:
            log.warning("ProviderAgentSession input queue full; dropping prompt")

    def send_input_bytes(self, data: bytes) -> None:
        # Not meaningful for structured-event sessions, but satisfies
        # SessionProtocol. Decode as UTF-8 and treat as text.
        try:
            self.send_input(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def resize(self, rows: int, cols: int, source: str | None = None,
               client_id: str | None = None) -> None:
        # Provider sessions don't have a TTY; remember dims for to_dict.
        self.rows = rows
        self.cols = cols

    def interrupt(self, timeout: float = 30.0) -> None:
        # Ask the provider to cancel, then watchdog: if the session is
        # still not finished after `timeout`, force-kill it. Without the
        # watchdog, a provider whose agent ignores the cancel (e.g. an
        # ACP adapter that doesn't honour session/cancel) leaves the
        # session stuck in "stopping" forever — no resume, no dismiss.
        self._was_graceful = True
        asyncio.ensure_future(self._do_interrupt(timeout))

    async def _do_interrupt(self, timeout: float = 30.0) -> None:
        try:
            await self._provider.interrupt()
        except Exception as e:
            log.warning("provider interrupt failed: %s", e)
        # Watchdog — escalate to a hard kill if the session hasn't
        # ended on its own within the grace period.
        try:
            await asyncio.sleep(max(1.0, timeout))
        except asyncio.CancelledError:
            return
        if self.status in ("running", "starting", "stopping"):
            log.warning(
                "ProviderAgentSession '%s' did not stop within %.0fs — "
                "forcing kill", self.id, timeout)
            await self.kill()

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

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

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

    def get_message_history(
        self, offset: int = 0, limit: int | None = None,
    ) -> list[dict]:
        if limit is None:
            return list(self._message_history[offset:])
        return list(self._message_history[offset:offset + limit])

    def get_message_count(self) -> int:
        return len(self._message_history)

    # ------------------------------------------------------------------
    # Runtime model & agent control (mirrors AgentSession.set_*_async API)
    # ------------------------------------------------------------------

    async def set_model_async(self, model: str) -> None:
        """Switch the active model for subsequent turns.

        Delegates to the provider, then updates agent_options and
        broadcasts a settings event so all subscribed clients refresh
        the header / model picker.
        """
        try:
            await self._provider.set_model(model)
        except NotImplementedError:
            return
        self._agent_options["model"] = model
        self._broadcast_event({
            "type": "settings",
            "model": model,
            "provider": self._provider.name,
        })

    async def set_effort(self, effort: str) -> None:
        # Most providers don'''t have an effort dial; accept and store
        # for the ones that might. No-op at the provider level for now.
        self._agent_options["effort"] = effort

    async def get_models(self) -> list[dict]:
        """Return the model catalogue from the underlying provider."""
        try:
            return await self._provider.list_models()
        except NotImplementedError:
            return []
        except Exception as e:
            log.warning("provider list_models failed: %s", e)
            return []

    async def get_context_usage(self) -> dict:
        try:
            return await self._provider.get_context_usage()
        except NotImplementedError:
            return {}
        except Exception:
            return {}

    def answer_question(self, answer: str) -> None:
        """Reply to the most recent permission_request event.

        v1: relies on the provider holding state (only one in-flight
        request at a time). The orchestrator tracks the most recent
        request_id from permission_request events.
        """
        try:
            request_id = self._last_permission_request_id
            asyncio.ensure_future(
                self._provider.respond_to_permission(request_id, answer),
            )
        except Exception:
            pass

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
        return [{"id": cid, "source": src}
                for cid, src in self._attached_sources.items()]

    @property
    def live_cwd(self) -> str | None:
        return self.cwd

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "session_type": self.session_type,
            "name": self.name,
            "command": self.command,
            "provider": self._provider.name,
            "capabilities": sorted(self._provider.capabilities),
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
        if self._agent_options:
            persisted_opts = {
                k: v for k, v in self._agent_options.items()
                if k in ("model", "agent")
            }
            if persisted_opts:
                d["agent_options"] = persisted_opts
        return d

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Start the provider, fan out its events, drain the input queue."""
        try:
            try:
                await self._provider.start()
            except Exception as e:
                self._emit_event({"type": "error", "error": str(e)})
                self.status = "exited"
                self.exit_code = 1
                self._broadcast_close()
                if self._on_exit:
                    await self._on_exit(self.id)
                return

            # Spawn the provider event consumer, then yield enough that
            # the system_init event (already on the provider's queue
            # from start()) reaches subscribers before any user_message
            # we'd emit from the input drain loop. Without this, a
            # client connecting and replaying history sees events in a
            # confusing order.
            self._stream_task = asyncio.create_task(self._consume_provider_events())
            for _ in range(3):
                await asyncio.sleep(0)

            # Send the initial prompt if one was given.
            if self.prompt and self.prompt.strip():
                self._input_queue.put_nowait({"text": self.prompt, "attachments": []})

            # Drain the input queue. Each entry is one user turn.
            while self.status == "running":
                try:
                    entry = await self._input_queue.get()
                except asyncio.CancelledError:
                    break

                if entry.get("_shutdown"):
                    break

                text = entry.get("text", "")
                attachments = entry.get("attachments") or []
                btw = bool(entry.get("btw"))

                self._turn_counter += 1
                turn_id = f"turn-{self._turn_prefix}-{self._turn_counter}"
                self._current_turn_id = turn_id

                # Echo the user prompt to subscribers + history. The
                # provider doesn't always emit user_message itself
                # quickly enough; doing it here keeps multi-client UIs
                # consistent.
                #
                # BTW prompts are tagged so `_emit_event` broadcasts
                # them without persisting to history — the side-channel
                # question/answer is transient by design.
                self._emit_event({
                    "type": "user_message",
                    "content": text,
                    "turn_id": turn_id,
                    "btw": btw,
                })

                self._processing = True
                try:
                    await self._provider.send(
                        text=text,
                        attachments=attachments,
                        model=self._agent_options.get("model"),
                        agent=self._agent_options.get("agent"),
                        options={"btw": btw} if btw else None,
                    )
                except Exception as e:
                    self._emit_event({"type": "error", "error": str(e)})
                finally:
                    self._processing = False

        except Exception as e:
            log.exception("ProviderAgentSession main loop fatal error")
            self._emit_event({"type": "error", "error": str(e)})
        finally:
            # Wind down provider + stream consumer.
            try:
                await self._provider.stop()
            except Exception:
                pass
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self.status not in ("exited", "killed"):
                self.status = "exited"
                self.exit_code = self.exit_code or 0
            self._emit_event({
                "type": "session_end",
                "exit_code": self.exit_code,
            })
            self._broadcast_close()
            saver = self._history_saver_task
            if saver and not saver.done():
                saver.cancel()
                try:
                    await saver
                except (asyncio.CancelledError, Exception):
                    pass
            if self._on_exit:
                await self._on_exit(self.id)

    async def _consume_provider_events(self) -> None:
        """Pull events from the provider; tag with turn_id; broadcast."""
        try:
            async for ev in self._provider.events():
                # Tag with current turn for grouping.
                if self._current_turn_id and "turn_id" not in ev:
                    ev["turn_id"] = self._current_turn_id
                # session_end is the orchestrator's own emission;
                # don't double-emit from the provider.
                if ev.get("type") == "session_end":
                    continue
                # Capture the provider's resume token from system_init.
                # Providers that support session persistence (e.g. the
                # ACP adapters, when the agent advertises loadSession)
                # put their resumable session id here; persisting it to
                # self.resume_id is what makes the session show up as
                # resumable after it exits.
                if ev.get("type") == "system_init":
                    rid = ev.get("resume_id")
                    if rid:
                        self.resume_id = rid
                # Track the most recent permission request so a later
                # answer_question call routes to the right id.
                if ev.get("type") == "permission_request":
                    rid = ev.get("request_id")
                    if rid:
                        self._last_permission_request_id = rid
                self._emit_event(dict(ev))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._emit_event({"type": "error", "error": f"event stream: {e}"})

    # ------------------------------------------------------------------
    # Broadcast / history machinery
    # ------------------------------------------------------------------

    def _emit_event(self, event: dict) -> None:
        """Broadcast an event to subscribers and append to history."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        if self._current_turn_id and "turn_id" not in event:
            event["turn_id"] = self._current_turn_id

        # Fan out first.
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropped event (queue full): %s",
                            event.get("type"))

        # Persist + console buffer (skip ephemeral btw markers).
        if not event.get("btw"):
            self._message_history.append(event)
            self._save_history()
        text = _format_event_ansi(event)
        if text:
            data = text.encode("utf-8", errors="replace")
            self._console_buffer.extend(data)
            if len(self._console_buffer) > BUFFER_MAX_BYTES:
                excess = len(self._console_buffer) - BUFFER_MAX_BYTES
                del self._console_buffer[:excess]

    def _broadcast_event(self, event: dict) -> None:
        """Broadcast without persisting (for transient signals)."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        if self._current_turn_id and "turn_id" not in event:
            event["turn_id"] = self._current_turn_id
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _broadcast_close(self) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    @staticmethod
    def _json_safe(obj: Any) -> Any:
        """Best-effort scrub for JSON-serialisable values."""
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            if isinstance(obj, dict):
                return {k: ProviderAgentSession._json_safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [ProviderAgentSession._json_safe(v) for v in obj]
            return str(obj)

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    def _history_path(self) -> Path:
        """Where to store this session's history JSON.

        Same directory the existing AgentSession uses (so the dashboard
        can find both kinds of session uniformly), with a different
        filename suffix to avoid collision.
        """
        base = Path.home() / ".be-conductor" / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{self.id}.history.json"

    def _load_history(self) -> None:
        path = self._history_path()
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                self._message_history = json.load(f)
        except Exception as e:
            log.warning("failed to load history for %s: %s", self.id, e)
            self._message_history = []

    def _save_history(self) -> None:
        """Mark history dirty; lazy background flush.

        Mirrors AgentSession's pattern: subscribers see events
        synchronously, disk writes happen on a background task.
        """
        self._history_dirty = True
        if self._history_saver_task is None or self._history_saver_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._history_saver_task = loop.create_task(self._history_saver())
            except RuntimeError:
                # No loop — write inline (e.g. test mode).
                self._flush_history()

    async def _history_saver(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._history_dirty:
                    self._flush_history()
                    self._history_dirty = False
                if self.status not in ("running", "starting"):
                    # One last flush on exit.
                    if self._history_dirty:
                        self._flush_history()
                    return
        except asyncio.CancelledError:
            if self._history_dirty:
                self._flush_history()
            raise

    def _flush_history(self) -> None:
        try:
            path = self._history_path()
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._message_history, f, ensure_ascii=False)
            tmp.replace(path)
        except Exception as e:
            log.warning("failed to flush history for %s: %s", self.id, e)
