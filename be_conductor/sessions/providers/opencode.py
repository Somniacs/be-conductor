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
# Per-URL singleton SSE pump
#
# The OpenCode server's `GET /event` is one logical event stream per
# server. Empirically (verified on opencode 1.14.39), opening more than
# one concurrent SSE subscriber against the same server causes events
# to vanish — only the first or last subscriber gets them, the others
# starve. So when multiple be-conductor sessions point at the same
# OpenCode server, they cannot each open their own subscriber.
#
# The pump below holds **one** subscription per (URL, password) and
# fans out events to per-session asyncio.Queues. Each provider
# registers its session id and pulls only events that match.
#
# A symptom of the previous code: a second simultaneous OpenCode
# session received `system_init` and `turn_end` markers (locally
# emitted by the orchestrator) but no `message.part.*` events
# in between, leaving turns visibly blank in the chat even though
# the model produced a real answer on the OpenCode side.
# ---------------------------------------------------------------------------


class _SsePump:
    """One SSE subscription per (OpenCode server, directory) pair,
    fanning events out to per-session queues.

    OpenCode 1.14.39's `GET /event` is **project-scoped** — events for
    a session are only delivered to subscribers that opened the stream
    with the same `directory` (= project) the session was created in.
    A single SSE subscriber can therefore only see events for sessions
    in one directory. If be-conductor has sessions in multiple
    directories, we need one pump per directory.
    """

    # How long to buffer events for a session id that hasn't attached
    # yet — covers the race between session.create returning the id and
    # pump.attach() being called.
    _EARLY_BUFFER_TTL = 5.0
    _EARLY_BUFFER_PER_SESSION = 200

    def __init__(
        self,
        base_url: str,
        password: str | None,
        directory: str | None,
    ) -> None:
        self._base_url = base_url
        self._password = password
        self._directory = directory
        self._client: Any = None  # opencode_ai.Opencode
        # session_id -> asyncio.Queue[dict]   (raw OpenCode events)
        self._subscribers: dict[str, asyncio.Queue] = {}
        # session_id -> [(monotonic_time, event_dict), ...]
        # Buffer for events whose sid we know but which haven't been
        # claimed by an attach() call yet. Drained on attach.
        self._early: dict[str, list[tuple[float, dict]]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def _ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # Lazy-construct the SDK client.
        from opencode_ai import Opencode
        headers: dict[str, str] = {}
        if self._password:
            headers["Authorization"] = f"Bearer {self._password}"
        self._client = Opencode(
            base_url=self._base_url,
            timeout=180,
            default_headers=headers or None,
        )
        self._task = asyncio.create_task(self._run())

    async def attach(self, session_id: str) -> asyncio.Queue:
        """Register a per-session queue and start the pump if needed."""
        async with self._lock:
            if session_id in self._subscribers:
                # Reattachment — drain the old queue but keep the same
                # one so currently-suspended consumers don't lose
                # their queue reference.
                return self._subscribers[session_id]
            q: asyncio.Queue = asyncio.Queue(maxsize=10000)
            self._subscribers[session_id] = q
            await self._ensure_started()
            # Drain any events buffered before this attach — these
            # arrive between session.create returning the id and the
            # caller getting around to attach(). Without this drain,
            # the message.updated events that set up role tracking
            # are silently dropped and parts arrive without a known
            # role, getting filtered out as "user".
            buffered = self._early.pop(session_id, None)
            if buffered:
                for _t, ev in buffered:
                    try:
                        q.put_nowait(ev)
                    except asyncio.QueueFull:
                        break
            return q

    async def detach(self, session_id: str) -> None:
        async with self._lock:
            self._subscribers.pop(session_id, None)
            if not self._subscribers and self._task is not None:
                self._task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._task
                self._task = None

    @property
    def client(self) -> Any:
        # The SDK client is shared; providers reuse it for session.create,
        # session.prompt, session.abort, session.permissions.respond, etc.
        return self._client

    async def _run(self) -> None:
        """The hot loop — open the subscription, dispatch events."""
        loop = asyncio.get_running_loop()
        try:
            # Pass directory= so OpenCode delivers events for sessions
            # in this project. Without it, the server filters us to
            # its own cwd's project and we see nothing for sessions
            # in other directories.
            kwargs = {}
            if self._directory:
                kwargs["directory"] = self._directory
            stream_cm = await loop.run_in_executor(
                None, lambda: self._client.event.list(**kwargs),
            )
        except Exception as e:
            log.warning("opencode SSE pump failed to open stream: %s", e)
            return

        try:
            stream_iter = stream_cm.__enter__()

            def next_event():
                try:
                    return next(iter(stream_iter))
                except StopIteration:
                    return None

            while True:
                ev = await loop.run_in_executor(None, next_event)
                if ev is None:
                    break
                evd = ev.model_dump() if hasattr(ev, "model_dump") else ev

                # Identify the session this event belongs to.
                props = evd.get("properties", {}) or {}
                sid = (
                    props.get("sessionID")
                    or (props.get("part") or {}).get("session_id")
                    or (props.get("info") or {}).get("session_id")
                )

                # Fan-out:
                #   - server-level events (no sid): broadcast to all
                #   - sid matches an attached subscriber: deliver
                #   - sid present but no subscriber yet: buffer for a
                #     short window (covers the attach race)
                async with self._lock:
                    if not sid:
                        targets = list(self._subscribers.values())
                        buffer_for: str | None = None
                    elif sid in self._subscribers:
                        targets = [self._subscribers[sid]]
                        buffer_for = None
                    else:
                        targets = []
                        buffer_for = sid

                if targets:
                    for q in targets:
                        try:
                            q.put_nowait(evd)
                        except asyncio.QueueFull:
                            log.warning(
                                "opencode SSE per-session queue full; dropping event %s",
                                evd.get("type"),
                            )
                elif buffer_for is not None:
                    now = asyncio.get_running_loop().time()
                    async with self._lock:
                        buf = self._early.setdefault(buffer_for, [])
                        # Trim old/oversized.
                        cutoff = now - self._EARLY_BUFFER_TTL
                        buf[:] = [(t, e) for (t, e) in buf if t >= cutoff]
                        if len(buf) >= self._EARLY_BUFFER_PER_SESSION:
                            buf.pop(0)
                        buf.append((now, evd))
        except asyncio.CancelledError:
            with suppress(Exception):
                stream_cm.__exit__(None, None, None)
            raise
        except Exception as e:
            log.warning("opencode SSE pump errored: %s", e)
        finally:
            with suppress(Exception):
                stream_cm.__exit__(None, None, None)


# (url, password, directory) -> pump
_pumps: dict[tuple[str, str | None, str | None], _SsePump] = {}
_pumps_lock = asyncio.Lock()


async def _get_pump(
    base_url: str,
    password: str | None,
    directory: str | None,
) -> _SsePump:
    key = (base_url, password, directory)
    async with _pumps_lock:
        pump = _pumps.get(key)
        if pump is None:
            pump = _SsePump(base_url, password, directory)
            _pumps[key] = pump
        return pump


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
        self._client: Any = None  # opencode_ai.Opencode (shared via pump)
        self._pump: _SsePump | None = None
        self._raw_q: asyncio.Queue | None = None  # raw OpenCode events for this session
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
            import opencode_ai  # noqa: F401  (raises ImportError if missing)
        except ImportError as e:
            raise RuntimeError(
                "opencode-ai is not installed. Run: "
                "`pip install -e '.[opencode]'` from the be-conductor repo."
            ) from e

        # Use the per-(URL, directory) singleton SSE pump and its shared
        # SDK client. Two reasons for the singleton:
        #   1) Multiple concurrent SSE subscribers to the same OpenCode
        #      server starve each other (only one wins).
        #   2) OpenCode 1.14.39's /event stream is project-scoped — we
        #      must pass `directory=` matching the session's directory
        #      or we get no events at all.
        # Pumps are keyed by directory so be-conductor sessions in
        # different cwds get their own subscription and don't collide.
        self._pump = await _get_pump(self._url, self._password, self._cwd)
        await self._pump._ensure_started()
        self._client = self._pump.client

        # Create a fresh session on the server, scoped to the
        # be-conductor session's working directory. Without `directory=`
        # OpenCode falls back to its own server process's cwd
        # (typically be-conductor's launch dir) so tools run in the
        # wrong place — bash's `pwd` reports be-conductor's path even
        # though the dashboard advertised the user-picked cwd in the
        # session header.
        loop = asyncio.get_running_loop()
        s = await loop.run_in_executor(
            None,
            lambda: self._client.session.create(directory=self._cwd),
        )
        self._session_id = s.id
        log.info("opencode: created session %s in %s", self._session_id, self._cwd)

        # Now that we know our session id, attach to the pump.
        self._raw_q = await self._pump.attach(self._session_id)

        # Emit the system_init event so the orchestrator can broadcast
        # capabilities + current model to the frontend.
        await self._event_queue.put({
            "type": "system_init",
            "provider": self.name,
            "capabilities": sorted(self.capabilities),
            "session_id": self._session_id,
            "subtype": "init",
            "model": f"{self._default_provider_id}/{self._default_model_id}",
            "agent": self._default_agent,
        })

        # Drain raw events from the pump and translate them into
        # AgentEvents on self._event_queue.
        self._stream_task = asyncio.create_task(self._consume_stream())

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True

        # End our local stream-translator task.
        if self._stream_task is not None:
            self._stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._stream_task

        # Detach from the singleton SSE pump. If we were the last
        # session attached, the pump's underlying SSE subscription is
        # closed too.
        if self._pump is not None and self._session_id is not None:
            with suppress(Exception):
                await self._pump.detach(self._session_id)

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
            # Scope tool execution to the user-picked cwd. session.create
            # already set this on the session, but passing it per-prompt
            # too makes it explicit and survives any server-side default
            # changes between OpenCode versions.
            "directory": self._cwd,
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

            # If the model failed (auth, rate limit, content filter,
            # etc.) OpenCode populates info.error and produces no text
            # parts. The session.error event in the SSE stream usually
            # carries the same info — but the SSE stream may race with
            # the prompt() return on short turns, so we double-check
            # here and emit a wire-protocol `error` event if needed.
            err_obj = getattr(info, "error", None)
            if err_obj is not None:
                err_dump = _safe_dump(err_obj) or {}
                name = err_dump.get("name") or type(err_obj).__name__
                data = err_dump.get("data") or {}
                msg = (
                    data.get("message")
                    or err_dump.get("message")
                    or name
                )
                if name and msg and name not in msg:
                    msg = f"{name}: {msg}"
                await self._event_queue.put({
                    "type": "error",
                    "error": str(msg or "OpenCode reported an error"),
                    "subtype": "provider_error",
                    "payload": {"raw": err_dump},
                })

            stop_reason = getattr(info, "finish", None)
            if not stop_reason:
                # No finish reason + no error means OpenCode aborted
                # silently. Tag the turn_end so the UI doesn't claim
                # the agent finished cleanly.
                stop_reason = "error" if err_obj is not None else "stop"

            await self._event_queue.put({
                "type": "turn_end",
                "stop_reason": stop_reason,
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
        # The orchestrator broadcasts a `settings` event so the frontend
        # header / picker refresh; we don't need to emit anything here.
        self._default_provider_id, self._default_model_id = self._parse_model(model)

    async def list_models(self) -> list[dict]:
        """Query OpenCode for the catalogue of models the user has
        access to (i.e. providers they've authenticated).

        Returns a flat list with one entry per model. The frontend
        renders these as "OpenCode • <provider> / <model>".
        """
        if self._client is None:
            return []
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, self._client.app.providers)
        except Exception as e:
            log.warning("opencode: list_models failed: %s", e)
            return []

        d = resp.model_dump() if hasattr(resp, "model_dump") else resp
        providers = d.get("providers") if isinstance(d, dict) else d
        if not isinstance(providers, list):
            return []

        current = f"{self._default_provider_id}/{self._default_model_id}"
        out: list[dict] = []
        for prov in providers:
            pid = prov.get("id") or ""
            pname = prov.get("name") or pid
            models = prov.get("models") or {}
            # `models` is a dict keyed by model id; the value carries
            # extra metadata (cost, context window, etc.) which we
            # currently ignore.
            if isinstance(models, dict):
                model_ids = list(models.keys())
            elif isinstance(models, list):
                model_ids = [m.get("id") if isinstance(m, dict) else str(m)
                             for m in models]
            else:
                model_ids = []
            for mid in model_ids:
                if not mid:
                    continue
                value = f"{pid}/{mid}"
                out.append({
                    "value": value,
                    "label": f"{pname} / {mid}",
                    "provider_id": pid,
                    "model_id": mid,
                    "current": value == current,
                })
        return out

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
        """Drain raw OpenCode events delivered by the singleton SSE
        pump (filtered to this session_id), translate to AgentEvents,
        push onto self._event_queue.

        Runs as a long-lived task. Cancelled by stop().
        """
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

        try:
            while not self._closed:
                if self._raw_q is None:
                    break
                evd = await self._raw_q.get()
                props = evd.get("properties", {}) or {}
                etype = evd.get("type", "")
                # Pump already filters to our session_id, but
                # cross-session "server.*" events may come through.
                # Skip anything not relevant to our session unless it's
                # a known server-level event we want to react to.
                sess_id = (
                    props.get("sessionID")
                    or (props.get("part") or {}).get("session_id")
                    or (props.get("info") or {}).get("session_id")
                )
                if sess_id and sess_id != self._session_id:
                    continue

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

                if etype == "session.error":
                    # OpenCode reports model-level / provider-level
                    # failures (auth errors, rate limits, content
                    # filtering, etc.) as session.error events with a
                    # nested error payload. We were silently dropping
                    # these — the symptom was an empty assistant
                    # bubble + zero-cost turn_end with no UI
                    # explanation. Surface as a wire `error` event
                    # so the user sees what went wrong.
                    err = props.get("error") or {}
                    if not isinstance(err, dict):
                        err = {"message": str(err)}
                    name = err.get("name") or ""
                    data = err.get("data") or {}
                    msg = (
                        data.get("message")
                        or err.get("message")
                        or name
                        or "OpenCode reported an error"
                    )
                    if name and name not in msg:
                        msg = f"{name}: {msg}"
                    await self._event_queue.put({
                        "type": "error",
                        "error": msg,
                        "subtype": "provider_error",
                        "payload": {"raw": err},
                    })
                    continue

                # Anything else — leave silent.

            # End-of-stream — flush any leftover deltas before exit.
            await flush_deltas()

        except asyncio.CancelledError:
            # Normal teardown — flush deltas so the last bit of
            # streamed text isn't lost.
            with suppress(Exception):
                await flush_deltas()
            raise
        except Exception as e:
            await self._event_queue.put({
                "type": "error",
                "error": f"opencode event stream errored: {e}",
            })


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
