"""OpenCode provider — adapts the `opencode-ai` Python SDK to AgentProvider.

Architecture:

    be-conductor session
        │
        ▼
    ProviderAgentSession (orchestrator — queue, history, broadcast)
        │
        ▼
    OpenCodeProvider  ── opencode-ai (REST client) ──▶  opencode serve (HTTP/SSE)
                                                             │
                                                             ▼
                                                    OpenAI / Anthropic / Google
                                                    (via OpenCode's routing)

The provider:
  - Lazy-imports `opencode-ai` at start() time (be-conductor still
    runs without the optional dep installed).
  - Auto-spawns `opencode serve --port 7798 --hostname 127.0.0.1`
    if no server is reachable at the configured URL. Reuses any
    existing server. Tracks PIDs we own so cleanup is precise.
  - Subscribes to OpenCode's global SSE event stream (one stream
    serves all sessions) and filters events to this session's
    `session_id`.
  - Translates OpenCode's `message.part.*` events into the wire
    protocol be-conductor's frontend already speaks
    (`assistant_message`, `tool_use_start/progress/end`,
    `stream_start/delta/stop`, etc.).

This module does **not** touch the existing Claude `AgentSession`
path. Importing it has no effect on existing sessions.

Configuration:

    BC_OPENCODE_URL          — server URL (default http://127.0.0.1:7798)
    BC_OPENCODE_PASSWORD     — bearer auth (optional; default none)
    BC_OPENCODE_AUTOSTART    — "true" / "false" (default true)
    BC_OPENCODE_PORT         — port for autospawn (default 7798)

See docs/planned/agent-abstraction.md (Tracks 1, 3) for the design
rationale and live-probe findings that produced this code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from be_conductor.sessions.providers.base import (
    AgentEvent,
    Capability,
    PROVIDER_NAME_OPENCODE,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle: shared `opencode serve` management
#
# Multiple be-conductor sessions can share a single OpenCode server. We
# track started subprocesses in a module-global so the *first* session
# starts the server and the *last* session shuts it down — but only if
# we started it ourselves.
# ---------------------------------------------------------------------------


_owned_subprocess: subprocess.Popen | None = None
_owned_url: str | None = None
_owned_lock = asyncio.Lock()
_session_refcount: int = 0


async def _ensure_server(url: str, *, autostart: bool, port: int) -> None:
    """Ensure an OpenCode server is reachable at `url`. Spawn if needed."""
    global _owned_subprocess, _owned_url, _session_refcount

    async with _owned_lock:
        # Bump refcount up front; we'll decrement on cleanup.
        _session_refcount += 1

        if await _is_reachable(url):
            log.info("opencode: using existing server at %s", url)
            return

        if not autostart:
            raise RuntimeError(
                f"opencode server not reachable at {url} and "
                "BC_OPENCODE_AUTOSTART is false"
            )

        opencode_bin = shutil.which("opencode")
        if not opencode_bin:
            raise RuntimeError(
                "opencode binary not found in PATH. Install it via "
                "`curl -fsSL https://opencode.ai/install | bash` or "
                "`npm i -g opencode-ai`."
            )

        log.info("opencode: spawning `opencode serve --port %d`", port)
        proc = subprocess.Popen(
            [opencode_bin, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Detach from controlling terminal so Ctrl-C in be-conductor
            # doesn't tear OpenCode down before we've cleaned up cleanly.
            start_new_session=True,
        )

        # Wait up to ~5s for it to come up.
        for _ in range(50):
            await asyncio.sleep(0.1)
            if await _is_reachable(url):
                _owned_subprocess = proc
                _owned_url = url
                log.info("opencode: server ready at %s (pid %d)", url, proc.pid)
                return

        # Couldn't reach it — clean up.
        with suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)
        raise RuntimeError(f"opencode server failed to start at {url} within 5s")


async def _release_server() -> None:
    """Decrement the session refcount. Tear down the server if we own
    it and no sessions remain."""
    global _owned_subprocess, _owned_url, _session_refcount

    async with _owned_lock:
        _session_refcount = max(0, _session_refcount - 1)
        if _session_refcount > 0:
            return
        if _owned_subprocess is None:
            return
        log.info("opencode: terminating owned server pid %d", _owned_subprocess.pid)
        with suppress(Exception):
            _owned_subprocess.terminate()
            _owned_subprocess.wait(timeout=3)
        _owned_subprocess = None
        _owned_url = None


async def _is_reachable(url: str) -> bool:
    """Cheap GET against /session — returns 200 with JSON when alive."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(url.rstrip("/") + "/session")
            return r.status_code == 200 and r.headers.get(
                "content-type", ""
            ).startswith("application/json")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# OpenCodeProvider
# ---------------------------------------------------------------------------


class OpenCodeProvider:
    """AgentProvider implementation backed by `opencode-ai`."""

    name: str = PROVIDER_NAME_OPENCODE

    def __init__(
        self,
        *,
        cwd: str | None = None,
        url: str | None = None,
        password: str | None = None,
        autostart: bool | None = None,
        default_provider_id: str = "openai",
        default_model_id: str = "gpt-5.5",
        default_agent: str = "build",
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._url = (url or os.environ.get("BC_OPENCODE_URL")
                     or "http://127.0.0.1:7798").rstrip("/")
        self._password = password or os.environ.get("BC_OPENCODE_PASSWORD") or None
        if autostart is None:
            env = os.environ.get("BC_OPENCODE_AUTOSTART", "true").lower()
            autostart = env in ("1", "true", "yes")
        self._autostart = autostart
        self._port = int(os.environ.get("BC_OPENCODE_PORT", "7798"))

        self._default_provider_id = default_provider_id
        self._default_model_id = default_model_id
        self._default_agent = default_agent

        # Capabilities advertised. See docs/planned/agent-abstraction.md
        # Track 2 fitness-check table for the rationale per flag.
        self.capabilities: set[str] = {
            Capability.TEXT,
            Capability.STREAMING_DELTAS,
            Capability.REASONING,
            Capability.TOOLS,
            Capability.TOOL_PROGRESS,
            Capability.MULTI_STEP_TURN,
            Capability.COST_REPORTING,
            Capability.TOKEN_USAGE,
            Capability.MODEL_SWITCHING,
            Capability.AGENT_SWITCHING,
            Capability.CANCEL,
            Capability.SESSION_RESUME,
            Capability.MID_TURN_APPROVAL,
            Capability.MCP,
            # Not advertised (would lie):
            #   PERMISSION_MODES, PRE_TOOL_APPROVAL, PLAN_REVIEW,
            #   EFFORT_LEVELS, ADAPTIVE_THINKING, COMPACT_BOUNDARY,
            #   RATE_LIMIT_EVENTS, SUBAGENTS, SKILLS, BTW_SIDECHANNEL,
            #   CONTEXT_USAGE
        }

        # Set in start()
        self._client: Any = None  # opencode_ai.Opencode
        self._session_id: str | None = None
        self._event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._stream_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False
        # Track which streaming text/reasoning parts have already
        # received a stream_start. The first message.part.updated for a
        # part is empty (we treat as stream_start); the second has the
        # full text (we treat as stream_stop). Without this set we'd
        # emit a spurious stream_stop on the empty first event.
        self._open_blocks: set[str] = set()
        # message_id → role ("user" / "assistant"). Populated from
        # message.updated events. Used to filter out parts belonging to
        # the user prompt — the orchestrator handles user echoes
        # separately and doesn't want them streamed.
        self._msg_role: dict[str, str] = {}

    # ----- lifecycle --------------------------------------------------

    async def start(self) -> None:
        # Auto-spawn server if needed.
        await _ensure_server(self._url, autostart=self._autostart, port=self._port)

        # Lazy import: be-conductor must run without `opencode-ai` installed.
        try:
            from opencode_ai import Opencode
        except ImportError as e:
            raise RuntimeError(
                "opencode-ai is not installed. Run: "
                "`pip install -e '.[opencode]'` from the be-conductor repo."
            ) from e

        headers: dict[str, str] = {}
        if self._password:
            headers["Authorization"] = f"Bearer {self._password}"
        self._client = Opencode(
            base_url=self._url,
            timeout=180,
            default_headers=headers or None,
        )

        # Create a fresh session on the server.
        loop = asyncio.get_running_loop()
        s = await loop.run_in_executor(None, self._client.session.create)
        self._session_id = s.id
        log.info("opencode: created session %s", self._session_id)

        # Emit the system_init event so the orchestrator can broadcast
        # capabilities to the frontend.
        await self._event_queue.put({
            "type": "system_init",
            "provider": self.name,
            "capabilities": sorted(self.capabilities),
            "session_id": self._session_id,
            "subtype": "init",
        })

        # Start the SSE event subscriber.
        self._stream_task = asyncio.create_task(self._consume_stream())

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True

        # End the SSE subscriber.
        if self._stream_task is not None:
            self._stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._stream_task

        # Best-effort delete on server (keeps things tidy).
        if self._client is not None and self._session_id is not None:
            loop = asyncio.get_running_loop()
            with suppress(Exception):
                await loop.run_in_executor(
                    None, self._client.session.delete, self._session_id,
                )

        # Final session_end event.
        await self._event_queue.put({
            "type": "session_end",
            "exit_code": 0,
        })

        await _release_server()

    async def interrupt(self) -> None:
        if self._client is None or self._session_id is None:
            return
        loop = asyncio.get_running_loop()
        with suppress(Exception):
            await loop.run_in_executor(
                None, self._client.session.abort, self._session_id,
            )

    # ----- input ------------------------------------------------------

    async def send(
        self,
        *,
        text: str,
        attachments: list[dict] | None = None,
        model: str | None = None,
        agent: str | None = None,
        options: dict | None = None,
    ) -> None:
        if self._client is None or self._session_id is None:
            raise RuntimeError("OpenCodeProvider.start() not called")

        # Translate model string (e.g. "openai/gpt-5.5") to
        # OpenCode's {providerID, modelID} dict shape.
        provider_id, model_id = self._parse_model(model)
        agent_name = agent or self._default_agent

        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        # Attachments — TODO future work: map be-conductor's attachment
        # dicts onto OpenCode's FilePart / FilePartSource shapes. For
        # v1 we ignore them. Documented as a known limitation.
        # TODO(phase-c): attachments → OpenCode FilePart conversion

        kwargs: dict[str, Any] = {
            "parts": parts,
            "model": {"providerID": provider_id, "modelID": model_id},
            "agent": agent_name,
        }

        async with self._send_lock:
            loop = asyncio.get_running_loop()
            # Announce turn start to subscribers.
            await self._event_queue.put({
                "type": "turn_start",
            })
            # `prompt()` blocks until the turn completes (multi-step
            # included). We rely on the event stream for live updates;
            # the return value gives us the final summary.
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.session.prompt(self._session_id, **kwargs),
                )
            except Exception as e:
                await self._event_queue.put({
                    "type": "error",
                    "error": str(e),
                })
                await self._event_queue.put({
                    "type": "turn_end",
                    "stop_reason": "error",
                })
                return

            # Synthesize a turn_end event with the final summary. The
            # individual deltas / parts have already streamed through
            # the SSE consumer.
            info = response.info
            await self._event_queue.put({
                "type": "turn_end",
                "stop_reason": getattr(info, "finish", "stop") or "stop",
                "total_cost_usd": float(getattr(info, "cost", 0.0) or 0.0),
                "usage": _safe_dump(getattr(info, "tokens", None)),
                "model_usage": {
                    "provider_id": getattr(info, "provider_id", provider_id),
                    "model_id": getattr(info, "model_id", model_id),
                    "agent": getattr(info, "agent", agent_name),
                },
            })

    # ----- output -----------------------------------------------------

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            ev = await self._event_queue.get()
            yield ev
            if ev.get("type") == "session_end":
                return

    # ----- optional capabilities --------------------------------------

    async def set_model(self, model: str) -> None:
        # OpenCode routes model per-call; just update the default.
        self._default_provider_id, self._default_model_id = self._parse_model(model)

    async def set_agent(self, agent: str) -> None:
        self._default_agent = agent

    async def get_context_usage(self) -> dict:
        # Not advertised — the abstraction's `Capability.CONTEXT_USAGE`
        # flag is absent. Orchestrator should never call this.
        raise NotImplementedError("OpenCode does not expose live context usage")

    async def respond_to_permission(
        self, request_id: str, decision: str,
    ) -> None:
        # `decision` is forwarded verbatim — valid values are
        # OpenCode's "once" / "always" / "reject". Orchestrator
        # translates frontend buttons before calling.
        if self._client is None or self._session_id is None:
            return
        loop = asyncio.get_running_loop()
        with suppress(Exception):
            await loop.run_in_executor(
                None,
                lambda: self._client.session.permissions.respond(
                    request_id,
                    id=self._session_id,
                    response=decision,
                ),
            )

    # ----- internals --------------------------------------------------

    def _parse_model(self, model: str | None) -> tuple[str, str]:
        if not model:
            return self._default_provider_id, self._default_model_id
        if "/" in model:
            p, m = model.split("/", 1)
            return p, m
        return self._default_provider_id, model

    async def _consume_stream(self) -> None:
        """Subscribe to OpenCode's global event stream, filter to our
        session, translate to AgentEvent, push onto the queue.

        Runs as a long-lived task. Cancelled by stop().
        """
        loop = asyncio.get_running_loop()

        # Coalescing buffers for stream_delta events. OpenCode emits
        # one event per token; without batching we'd flood the
        # WebSocket the same way the Claude path used to before
        # `_flush_stream_buffers` was added.
        delta_buf: dict[tuple[str, str], str] = {}

        async def flush_deltas():
            for (part_id, field), text in list(delta_buf.items()):
                if not text:
                    continue
                if field == "text":
                    await self._event_queue.put({
                        "type": "stream_delta",
                        "delta_type": "text",
                        "text": text,
                        "tool_use_id": part_id,  # reuse for part-id correlation
                    })
                elif field == "reasoning":
                    await self._event_queue.put({
                        "type": "stream_delta",
                        "delta_type": "thinking",
                        "thinking": text,
                        "tool_use_id": part_id,
                    })
                delta_buf[(part_id, field)] = ""
            delta_buf.clear()

        def open_stream():
            return self._client.event.list()

        try:
            stream_cm = await loop.run_in_executor(None, open_stream)
        except Exception as e:
            await self._event_queue.put({
                "type": "error",
                "error": f"opencode event stream failed to open: {e}",
            })
            return

        try:
            # The SDK's Stream is a context manager around an iterator.
            # Iterate in a thread to avoid blocking the event loop.
            stream_iter = stream_cm.__enter__()

            def next_event():
                try:
                    return next(iter(stream_iter))
                except StopIteration:
                    return None

            while not self._closed:
                ev = await loop.run_in_executor(None, next_event)
                if ev is None:
                    break
                evd = ev.model_dump() if hasattr(ev, "model_dump") else ev

                # Filter to our session.
                props = evd.get("properties", {}) or {}
                sess_id = (
                    props.get("sessionID")
                    or (props.get("part") or {}).get("session_id")
                    or (props.get("info") or {}).get("session_id")
                )
                # Server-level events (server.connected, server.heartbeat)
                # have no session — skip.
                if sess_id and sess_id != self._session_id:
                    continue

                etype = evd.get("type", "")

                # Translate OpenCode events to AgentEvents.
                # Strategy: keep it small and faithful. Every event we
                # don't explicitly recognize is dropped silently
                # (server.heartbeat, session.diff, etc.).

                # Track message roles so we can filter out parts that
                # belong to the user prompt — those would otherwise
                # appear as duplicate stream_start events.
                if etype == "message.updated":
                    info = props.get("info") or {}
                    mid = info.get("id")
                    role = info.get("role")
                    if mid and role:
                        self._msg_role[mid] = role
                    continue

                # Helper: is this part part of an assistant message?
                def _is_assistant_part(p: dict) -> bool:
                    mid = p.get("message_id") or p.get("messageID")
                    # Default to assistant if role is unknown — better
                    # to render than to silently drop. Role normally
                    # arrives before part events in practice.
                    return self._msg_role.get(mid, "assistant") == "assistant"

                if etype == "message.part.delta":
                    field = props.get("field")
                    delta = props.get("delta", "")
                    pid = props.get("partID", "")
                    mid = props.get("messageID", "")
                    # Skip user-message deltas (rare but possible).
                    if mid and self._msg_role.get(mid) == "user":
                        continue
                    if field in ("text", "reasoning") and delta:
                        key = (pid, field)
                        delta_buf[key] = delta_buf.get(key, "") + delta
                        # Flush opportunistically when buffer per-key
                        # exceeds ~80 chars or a small interval — keeps
                        # the UI responsive without flooding.
                        if len(delta_buf[key]) >= 80:
                            await flush_deltas()
                    continue

                if etype == "message.part.updated":
                    part = props.get("part") or {}
                    if not _is_assistant_part(part):
                        continue
                    pt = part.get("type")
                    pid = part.get("id", "")

                    if pt in ("text", "reasoning"):
                        # The first `message.part.updated` for a
                        # text/reasoning part has empty body — that's
                        # our stream_start signal. Subsequent updates
                        # with non-empty text mean the streaming run
                        # is complete (deltas have already been
                        # flushed). Track which parts we've seen.
                        block_type = "text" if pt == "text" else "thinking"
                        if pid not in self._open_blocks:
                            self._open_blocks.add(pid)
                            await self._event_queue.put({
                                "type": "stream_start",
                                "tool_use_id": pid,
                                "block_type": block_type,
                            })
                        elif part.get("text"):
                            # Flush remaining deltas, then emit stop.
                            await flush_deltas()
                            self._open_blocks.discard(pid)
                            await self._event_queue.put({
                                "type": "stream_stop",
                                "tool_use_id": pid,
                                "block_type": block_type,
                            })

                    elif pt == "tool":
                        state = part.get("state") or {}
                        status = state.get("status")
                        tool_name = part.get("tool", "")
                        tu_id = part.get("id", "")
                        if status == "pending":
                            await self._event_queue.put({
                                "type": "tool_use_start",
                                "tool": tool_name,
                                "tool_use_id": tu_id,
                                "input": state.get("input") or {},
                            })
                        elif status == "running":
                            await self._event_queue.put({
                                "type": "tool_use_progress",
                                "tool": tool_name,
                                "tool_use_id": tu_id,
                                "input": state.get("input") or {},
                                "output": (state.get("metadata") or {}).get("output", ""),
                            })
                        elif status == "completed":
                            await self._event_queue.put({
                                "type": "tool_use_end",
                                "tool": tool_name,
                                "tool_use_id": tu_id,
                                "input": state.get("input") or {},
                                "output": state.get("output") or "",
                                "is_error": False,
                            })
                        elif status == "error":
                            await self._event_queue.put({
                                "type": "tool_use_end",
                                "tool": tool_name,
                                "tool_use_id": tu_id,
                                "input": state.get("input") or {},
                                "output": str(state.get("error") or ""),
                                "is_error": True,
                            })
                    # step-start / step-finish / file / snapshot —
                    # not surfaced to the wire protocol for v1.
                    continue

                if etype.startswith("permission."):
                    # OpenCode's permission flow. v1: surface the
                    # `permission.asked` event as `permission_request`
                    # so the existing be-conductor question modal can
                    # render it. Reply via respond_to_permission().
                    if etype == "permission.asked":
                        info = props.get("info") or props.get("permission") or {}
                        await self._event_queue.put({
                            "type": "permission_request",
                            "request_id": info.get("id", ""),
                            "tool": info.get("tool", ""),
                            "input": info.get("input") or {},
                            "payload": info,
                        })
                    elif etype == "permission.replied":
                        info = props.get("info") or props.get("permission") or {}
                        await self._event_queue.put({
                            "type": "permission_resolved",
                            "request_id": info.get("id", ""),
                            "decision": info.get("response") or info.get("reply") or "",
                        })
                    continue

                # Anything else — leave silent.

            # End-of-stream — flush any leftover deltas before exit.
            await flush_deltas()

        except asyncio.CancelledError:
            # Normal teardown.
            with suppress(Exception):
                stream_cm.__exit__(None, None, None)
            raise
        except Exception as e:
            await self._event_queue.put({
                "type": "error",
                "error": f"opencode event stream errored: {e}",
            })
        finally:
            with suppress(Exception):
                stream_cm.__exit__(None, None, None)


def _safe_dump(obj: Any) -> dict:
    """Best-effort pydantic→dict for telemetry fields."""
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return {}
    if isinstance(obj, dict):
        return obj
    return {}
