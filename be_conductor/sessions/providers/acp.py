"""ACP provider — drives any Agent Client Protocol agent over stdio.

Architecture:

    be-conductor session
        │
        ▼
    ProviderAgentSession (orchestrator — queue, history, broadcast)
        │
        ▼
    AcpProvider  ── JSON-RPC 2.0 / stdio ──▶  ACP adapter subprocess
                                                  (npx -y …)
                                                      │
                                                      ▼
                                              Claude / Codex / Gemini …

ACP (https://agentclientprotocol.com) is a *protocol*, not a vendor
SDK: one transport implementation drives ~40 agents. Each agent is
reached through a small adapter binary that speaks ACP on its stdin /
stdout. We launch the adapter as a subprocess and exchange
newline-delimited JSON-RPC 2.0 messages with it.

Unlike the OpenCode provider (which is tied to one vendor's REST SDK),
this provider is agent-agnostic. The only per-agent knowledge lives in
``ACP_AGENTS`` below — a launch command. ``acp-claude``, ``acp-codex``
and ``acp-gemini`` are all *this same class*; they differ only by which
adapter subprocess gets spawned.

Design notes — see docs/planned/agent-abstraction.md:

  - ACP is the *breadth* layer. Native Claude (claude_agent_sdk) stays
    the *depth* path — ACP is a lowest-common-denominator by design and
    does not expose Claude-specific niceties (effort, adaptive
    thinking, compact boundaries, plan review, subagents, BTW). That is
    deliberate: ``acp-claude`` is the portable path, ``claude`` the
    full one.
  - Tool execution is *client-side*: ACP agents call back to us via
    ``fs/read_text_file`` / ``fs/write_text_file`` / ``terminal/*`` and
    we run those ourselves, so worktree isolation, the diff view and
    cwd-scoping behave exactly like the Claude path.
  - Session persistence uses ACP's spec'd ``session/load`` (the agent
    replays history on load). ``SESSION_RESUME`` is advertised only
    when the agent's ``initialize`` handshake reports ``loadSession``.

This module does **not** touch the Claude ``AgentSession`` path or the
OpenCode provider. Importing it has no side effects and no runtime
dependency beyond the stdlib (the transport is plain JSON over pipes).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid as _uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator

from be_conductor.sessions.providers.base import AgentEvent, Capability

log = logging.getLogger(__name__)

# ACP MAJOR protocol version we speak. Bump only on breaking changes.
ACP_PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Adapter registry
#
# The only per-agent knowledge in this module. Each entry describes how
# to launch an ACP adapter: `npx_args` are the arguments passed to `npx`
# (resolved at launch via acp_npx() — it is `npx.cmd` on Windows). `npx`
# fetches the adapter from the npm registry on first use and caches it;
# `be-conductor setup-acp` warms that cache up front.
#
# Package names (verified against the npm registry, May 2026):
#   - Claude: @agentclientprotocol/claude-agent-acp — the current,
#     maintained adapter (v0.33.x). It wraps the official Claude Code
#     SDK. The older @zed-industries/claude-code-acp (v0.16.x) is the
#     stale predecessor — do not use it.
#   - Codex:  @zed-industries/codex-acp — the maintained Codex adapter
#     (v0.14.x); the @agentclientprotocol/codex-acp package is still at
#     v0.0.x and not ready.
#   - Gemini: the Gemini CLI itself speaks ACP via `--experimental-acp`;
#     no separate adapter package is needed.
#
# ALL of these need Node.js >= 20 (the Gemini CLI declares it; the
# Claude / Codex SDKs require it in practice). `be-conductor doctor`
# checks this — see docs/acp.md.
#
# `cli` is the underlying agent CLI an adapter inherits its login from;
# `setup-acp` / `doctor` report whether it is on PATH.
# ---------------------------------------------------------------------------

ACP_AGENTS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "ACP: Claude",
        "npm": "@agentclientprotocol/claude-agent-acp",
        "npx_args": ["-y", "@agentclientprotocol/claude-agent-acp"],
        "cli": "claude",
    },
    "codex": {
        "label": "ACP: Codex",
        "npm": "@zed-industries/codex-acp",
        "npx_args": ["-y", "@zed-industries/codex-acp"],
        "cli": "codex",
    },
    "gemini": {
        "label": "ACP: Gemini",
        "npm": "@google/gemini-cli",
        "npx_args": ["-y", "@google/gemini-cli", "--experimental-acp"],
        "cli": "gemini",
    },
}

# Minimum Node.js major version required by every ACP adapter.
ACP_MIN_NODE_MAJOR = 20


# ---------------------------------------------------------------------------
# Environment detection — Node / npx
#
# These helpers are deliberately module-level and side-effect-free so the
# CLI (`be-conductor doctor` / `setup-acp`) can reuse them without
# constructing a provider. All are cross-platform: `shutil.which`
# resolves `npx.cmd` / `node.exe` on Windows automatically.
# ---------------------------------------------------------------------------


def acp_npx() -> str | None:
    """Return the full path to the `npx` executable, or None.

    On Windows this resolves `npx.cmd`; elsewhere `npx`. Used to build
    adapter launch commands — passing the resolved path (not the bare
    string "npx") is what makes subprocess spawning work on Windows.
    """
    return shutil.which("npx")


def find_node() -> str | None:
    """Return the full path to the `node` executable, or None."""
    return shutil.which("node")


def node_version() -> tuple[int, int, int] | None:
    """Return Node's (major, minor, patch), or None if node is absent
    or its version can't be parsed."""
    node = find_node()
    if not node:
        return None
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", (out.stdout or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _node_install_hint() -> str:
    """OS-appropriate one-liner for installing a recent Node.js."""
    if sys.platform == "win32":
        return "winget install OpenJS.NodeJS  (or: choco install nodejs)"
    if sys.platform == "darwin":
        return "brew install node"
    return "use your distro's package manager, or https://nodejs.org/"


def acp_preflight() -> str | None:
    """Check that the environment can run ACP adapters.

    Returns None when everything is in order, or a human-readable error
    string naming the problem and the fix. Cheap enough to call on every
    session start.
    """
    if acp_npx() is None:
        return (
            "Node.js / npx not found — ACP agents need Node.js "
            f"{ACP_MIN_NODE_MAJOR}+. Install it ({_node_install_hint()}), "
            "then run: be-conductor setup-acp"
        )
    ver = node_version()
    if ver is None:
        return (
            "Node.js could not be run — ACP agents need Node.js "
            f"{ACP_MIN_NODE_MAJOR}+. Install it ({_node_install_hint()}), "
            "then run: be-conductor setup-acp"
        )
    if ver[0] < ACP_MIN_NODE_MAJOR:
        return (
            f"Node.js {ver[0]}.{ver[1]}.{ver[2]} is too old — ACP agents "
            f"need Node.js {ACP_MIN_NODE_MAJOR}+. Upgrade it "
            f"({_node_install_hint()}), then run: be-conductor setup-acp"
        )
    return None


def agent_cli_status(agent_key: str) -> bool:
    """Return True if the underlying CLI for an ACP agent is on PATH.

    e.g. for `claude` this checks the `claude` CLI — the ACP adapter
    inherits that CLI's login, so its presence is a good readiness hint.
    """
    meta = ACP_AGENTS.get(agent_key)
    if not meta:
        return False
    return shutil.which(meta["cli"]) is not None


def acp_launch_command(agent_key: str) -> list[str]:
    """Build the full launch command (npx + args) for an ACP agent.

    Raises ValueError for an unknown agent, RuntimeError when npx is not
    available. The first element is the resolved npx path so the command
    spawns correctly on Windows.
    """
    meta = ACP_AGENTS.get(agent_key)
    if not meta:
        raise ValueError(f"unknown ACP agent: {agent_key!r}")
    npx = acp_npx()
    if npx is None:
        raise RuntimeError(
            "npx not found — install Node.js "
            f"{ACP_MIN_NODE_MAJOR}+ ({_node_install_hint()})"
        )
    return [npx, *meta["npx_args"]]


def list_acp_agents() -> list[dict[str, Any]]:
    """Return the catalogue of ACP agents for the new-session UI.

    Each entry carries readiness hints the dashboard uses:
      - `cli_signed_in` — the agent's underlying CLI is on PATH.
      - `ready` — Node.js is OK *and* the agent's CLI is present.
    Node-level readiness is reported once at the top of the response
    by `acp_environment_status()`.
    """
    node_ok = acp_preflight() is None
    out: list[dict[str, Any]] = []
    for key, meta in ACP_AGENTS.items():
        cli_ok = agent_cli_status(key)
        out.append({
            "id": f"acp-{key}",
            "key": key,
            "label": meta["label"],
            "cli": meta["cli"],
            "cli_signed_in": cli_ok,
            "ready": node_ok and cli_ok,
        })
    return out


def acp_environment_status() -> dict[str, Any]:
    """Return Node/npx readiness for the ACP feature as a whole."""
    ver = node_version()
    return {
        "node_ok": acp_preflight() is None,
        "node_version": (".".join(str(p) for p in ver) if ver else None),
        "npx_found": acp_npx() is not None,
        "min_node_major": ACP_MIN_NODE_MAJOR,
    }


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 stdio transport
#
# ACP frames messages as newline-delimited JSON on the adapter's stdin
# (we write) and stdout (we read). This class owns the subprocess, a
# background reader task, request/response correlation by `id`, and a
# dispatch hook for inbound notifications + inbound (agent→client)
# requests.
# ---------------------------------------------------------------------------


class _JsonRpcTransport:
    """Newline-delimited JSON-RPC 2.0 over a subprocess's stdio."""

    def __init__(
        self,
        cmd: list[str],
        cwd: str | None,
        env: dict[str, str] | None,
    ) -> None:
        self._cmd = cmd
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 0
        # id -> Future awaiting the matching response
        self._pending: dict[int, asyncio.Future] = {}
        # Handlers set by the provider before start():
        #   on_notification(method, params)
        #   on_request(method, params) -> result  (async)
        #   on_disconnect()  — adapter process exited unexpectedly
        self.on_notification = None  # async callable
        self.on_request = None       # async callable
        self.on_disconnect = None    # async callable, no args
        self._closed = False
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the adapter subprocess and begin reading.

        `_cmd` is expected to be fully resolved already (see
        acp_launch_command) — its first element is an absolute npx
        path, which is what lets the spawn succeed on Windows.
        """
        if not self._cmd or not Path(self._cmd[0]).exists() and \
                not shutil.which(self._cmd[0]):
            raise RuntimeError(
                f"ACP adapter launcher not found: {self._cmd[0] if self._cmd else '?'}. "
                "Run: be-conductor setup-acp"
            )
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        self._closed = True
        # Fail any in-flight requests so awaiting callers don't hang.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("ACP transport closed"))
        self._pending.clear()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if self._proc is not None and self._proc.returncode is None:
            with suppress(ProcessLookupError, Exception):
                self._proc.terminate()
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            if self._proc.returncode is None:
                with suppress(ProcessLookupError, Exception):
                    self._proc.kill()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def request(self, method: str, params: dict | None = None,
                       timeout: float | None = None) -> Any:
        """Send a JSON-RPC request and await its result."""
        if self._closed or not self.alive:
            raise RuntimeError("ACP transport not running")
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._write({
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params or {},
        })
        try:
            if timeout:
                return await asyncio.wait_for(fut, timeout=timeout)
            return await fut
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._closed or not self.alive:
            return
        await self._write({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    async def _write(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        async with self._write_lock:
            with suppress(ConnectionResetError, BrokenPipeError, Exception):
                self._proc.stdin.write(line.encode("utf-8"))
                await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON, demux responses / notifications /
        inbound requests."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break  # adapter closed stdout — process is exiting
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Some adapters print non-JSON banners to stdout.
                    log.debug("ACP: non-JSON stdout line ignored: %s", line[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ACP read loop errored: %s", e)
        finally:
            # Reader ended — the adapter process has exited or closed
            # its stdout. Unblock anything still waiting on a response.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(
                        "the ACP agent process exited unexpectedly"))
            # Notify the provider so it can emit a clean session_end
            # instead of leaving events() hanging on an empty queue.
            cb = self.on_disconnect
            if cb is not None:
                with suppress(Exception):
                    await cb()

    async def _dispatch(self, msg: dict) -> None:
        # Response to one of our requests.
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(
                        _extract_error_text(msg["error"])
                    ))
                else:
                    fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        if method is None:
            return

        # Inbound request (agent → client) — needs a response.
        if "id" in msg:
            result: Any = None
            error: dict | None = None
            try:
                if self.on_request is not None:
                    result = await self.on_request(method, msg.get("params") or {})
                else:
                    error = {"code": -32601, "message": f"Method not found: {method}"}
            except Exception as e:
                error = {"code": -32603, "message": str(e)}
            reply: dict = {"jsonrpc": "2.0", "id": msg["id"]}
            if error is not None:
                reply["error"] = error
            else:
                reply["result"] = result
            await self._write(reply)
            return

        # Inbound notification (agent → client) — fire and forget.
        if self.on_notification is not None:
            with suppress(Exception):
                await self.on_notification(method, msg.get("params") or {})

    async def _drain_stderr(self) -> None:
        """Keep the adapter's stderr pipe drained; log it at debug."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.debug("ACP[stderr]: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


# ---------------------------------------------------------------------------
# AcpProvider
# ---------------------------------------------------------------------------


class AcpProvider:
    """AgentProvider implementation backed by an ACP adapter subprocess."""

    def __init__(
        self,
        *,
        agent_key: str,
        cwd: str | None = None,
        resume_session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        if agent_key not in ACP_AGENTS:
            raise ValueError(f"unknown ACP agent: {agent_key!r}")
        self._agent_key = agent_key
        self.name: str = f"acp-{agent_key}"
        self._cwd = cwd or os.getcwd()
        self._resume_session_id = resume_session_id
        self._extra_env = extra_env

        # Capabilities are finalised after the initialize handshake (we
        # learn loadSession / promptCapabilities from the agent). Start
        # with the always-true baseline; start() augments this set.
        self.capabilities: set[str] = {
            Capability.TEXT,
            Capability.STREAMING_DELTAS,
            Capability.TOOLS,
            Capability.TOOL_PROGRESS,
            Capability.MULTI_STEP_TURN,
            Capability.CANCEL,
            Capability.PRE_TOOL_APPROVAL,
            Capability.MID_TURN_APPROVAL,
            # BTW is a be-conductor-side feature, not an ACP one: a BTW
            # prompt is sent as an ordinary session/prompt, but every
            # event it produces is tagged `btw` so the orchestrator
            # broadcasts it without persisting to history. Works for
            # every ACP agent with no protocol support needed.
            Capability.BTW_SIDECHANNEL,
            # Negotiated in start(): SESSION_RESUME, REASONING, MCP,
            #   SKILLS. Never advertised (ACP doesn't spec them):
            #   COST_REPORTING, TOKEN_USAGE, CONTEXT_USAGE,
            #   PERMISSION_MODES, EFFORT_LEVELS, ADAPTIVE_THINKING,
            #   COMPACT_BOUNDARY, RATE_LIMIT_EVENTS, SUBAGENTS,
            #   MODEL_SWITCHING, AGENT_SWITCHING.
        }

        # Set in start()
        self._transport: _JsonRpcTransport | None = None
        self._session_id: str | None = None
        self._agent_caps: dict[str, Any] = {}
        self._auth_methods: list[dict] = []
        self._event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._available_commands: list[dict] = []
        # Set true for the duration of a /btw turn. While true, every
        # event emitted is stamped `btw` so the orchestrator broadcasts
        # it transiently without writing it to history.
        self._btw_turn = False
        # The block_type ("text" / "thinking") of the streaming block
        # currently open, or None. ACP delivers raw message chunks with
        # no explicit block boundaries; we synthesise stream_start /
        # stream_stop from the first delta and the turn end so the
        # frontend creates a fresh assistant bubble per turn. Without
        # this, a new turn's text streams into the previous turn's
        # bubble.
        self._open_block: str | None = None
        # Accumulators for the current turn's assistant output. ACP only
        # streams chunks — it never sends a final "assistant_message".
        # We build one at turn_end so the turn is persisted as a
        # complete message; without it, replaying a stored ACP session
        # (the stream_* events are skipped during replay) shows every
        # assistant answer blank.
        self._turn_text = ""
        self._turn_thinking = ""

        # Tool-call bookkeeping: ACP tool_call_update carries only
        # changed fields, so we keep the last-known title/kind/input
        # per toolCallId to emit complete be-conductor tool events.
        self._tool_calls: dict[str, dict] = {}
        # Permission request id (the JSON-RPC request id) -> the Future
        # the dispatch handler is awaiting so respond_to_permission()
        # can resolve it. Also maps to the available option ids.
        self._pending_permissions: dict[str, dict] = {}

    # ----- lifecycle --------------------------------------------------

    async def start(self) -> None:
        # Preflight: fail fast with an actionable message instead of
        # spawning a doomed subprocess that hangs. acp_preflight()
        # checks for npx and a recent-enough Node.js.
        problem = acp_preflight()
        if problem:
            raise RuntimeError(f"{ACP_AGENTS[self._agent_key]['label']}: {problem}")

        env = dict(os.environ)
        if self._extra_env:
            env.update(self._extra_env)

        # Build the launch command via acp_launch_command() so the npx
        # path is resolved (npx.cmd on Windows).
        cmd = acp_launch_command(self._agent_key)
        self._transport = _JsonRpcTransport(cmd, self._cwd, env)
        self._transport.on_notification = self._on_notification
        self._transport.on_request = self._on_request
        self._transport.on_disconnect = self._on_disconnect
        await self._transport.start()

        # 1) initialize handshake — declare what *we* can do, learn what
        #    the agent can do.
        init_result = await self._transport.request("initialize", {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            "clientInfo": {
                "name": "be-conductor",
                "title": "be-conductor",
                "version": _bc_version(),
            },
        }, timeout=60)
        init_result = init_result or {}
        self._agent_caps = init_result.get("agentCapabilities") or {}
        self._auth_methods = init_result.get("authMethods") or []
        self._negotiate_capabilities()

        # 2) authenticate if the agent requires it. ACP agents that wrap
        #    an already-logged-in CLI (Claude, Gemini, Codex) usually
        #    report no auth methods — the adapter inherits the CLI's
        #    credentials. If methods are listed we pick the first.
        if self._auth_methods:
            method_id = self._auth_methods[0].get("id") or self._auth_methods[0].get("methodId")
            if method_id:
                with suppress(Exception):
                    await self._transport.request(
                        "authenticate", {"methodId": method_id}, timeout=120)

        # 3) create or load the session.
        if self._resume_session_id and Capability.SESSION_RESUME in self.capabilities:
            try:
                await self._transport.request("session/load", {
                    "sessionId": self._resume_session_id,
                    "cwd": self._cwd,
                    "mcpServers": [],
                }, timeout=120)
                self._session_id = self._resume_session_id
                log.info("ACP[%s]: loaded session %s", self.name,
                         self._session_id)
            except Exception as e:
                log.warning("ACP[%s]: session/load failed (%s); "
                            "starting fresh", self.name, e)
                self._resume_session_id = None

        if self._session_id is None:
            new_result = await self._transport.request("session/new", {
                "cwd": self._cwd,
                "mcpServers": [],
            }, timeout=120)
            new_result = new_result or {}
            self._session_id = new_result.get("sessionId")
            if not self._session_id:
                raise RuntimeError(
                    f"ACP[{self.name}]: session/new returned no sessionId")
            log.info("ACP[%s]: created session %s in %s", self.name,
                     self._session_id, self._cwd)

        # Emit system_init so the orchestrator broadcasts capabilities.
        # `resume_id` is only set when the agent advertised loadSession
        # — otherwise the UI must not offer Resume (the stored chat
        # history is still kept and replayed read-only).
        init_event: AgentEvent = {
            "type": "system_init",
            "provider": self.name,
            "capabilities": sorted(self.capabilities),
            "session_id": self._session_id,
            "subtype": "init",
            "model": self.name,
        }
        if Capability.SESSION_RESUME in self.capabilities:
            init_event["resume_id"] = self._session_id
        await self._event_queue.put(init_event)

    def _negotiate_capabilities(self) -> None:
        """Augment the capability set from the initialize response."""
        caps = self._agent_caps
        if caps.get("loadSession"):
            self.capabilities.add(Capability.SESSION_RESUME)
        # ACP doesn't separate reasoning text, but several adapters
        # stream `agent_thought_chunk`; advertise REASONING optimistically
        # — the frontend simply renders nothing if no thought chunks come.
        self.capabilities.add(Capability.REASONING)
        mcp = caps.get("mcpCapabilities") or {}
        if mcp.get("http") or mcp.get("sse"):
            self.capabilities.add(Capability.MCP)
        # available_commands_update notifications drive slash commands.
        self.capabilities.add(Capability.SKILLS)
        # Some adapters (Codex) stream `usage_update` with context-window
        # figures. Advertise CONTEXT_USAGE optimistically — the ring
        # just stays empty for adapters that don't send it.
        self.capabilities.add(Capability.CONTEXT_USAGE)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._transport is not None and self._session_id is not None:
            with suppress(Exception):
                await self._transport.request(
                    "session/close", {"sessionId": self._session_id},
                    timeout=5)
        if self._transport is not None:
            with suppress(Exception):
                await self._transport.stop()
        await self._event_queue.put({"type": "session_end", "exit_code": 0})

    async def interrupt(self) -> None:
        if self._transport is None or self._session_id is None:
            return
        with suppress(Exception):
            await self._transport.notify(
                "session/cancel", {"sessionId": self._session_id})

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
        if self._transport is None or self._session_id is None:
            raise RuntimeError("AcpProvider.start() not called")

        prompt_blocks = self._build_prompt_blocks(text or "", attachments or [])

        # /btw side-channel: tag every event of this turn so the
        # orchestrator shows it transiently without persisting it.
        self._btw_turn = bool((options or {}).get("btw"))
        async with self._send_lock:
            try:
                await self._emit({"type": "turn_start"})
                try:
                    # session/prompt blocks until the whole turn (all
                    # tool-call round-trips included) completes; live
                    # updates arrive as session/update notifications.
                    result = await self._transport.request("session/prompt", {
                        "sessionId": self._session_id,
                        "prompt": prompt_blocks,
                    })
                except Exception as e:
                    await self._close_stream_block()
                    await self._emit_assistant_message()
                    await self._emit({"type": "error", "error": str(e)})
                    await self._emit({
                        "type": "turn_end", "stop_reason": "error"})
                    return

                # Close the streaming block so the frontend finalises
                # this turn's bubble, then emit a complete
                # assistant_message snapshot so the turn survives a
                # history reload (stream_* events are skipped on replay).
                await self._close_stream_block()
                await self._emit_assistant_message()
                stop_reason = (result or {}).get("stopReason", "end_turn")
                await self._emit({
                    "type": "turn_end",
                    "stop_reason": stop_reason,
                })
            finally:
                self._btw_turn = False
                self._turn_text = ""
                self._turn_thinking = ""

    def _build_prompt_blocks(
        self, text: str, attachments: list[dict],
    ) -> list[dict]:
        """Translate be-conductor's attachment dicts into ACP
        ContentBlocks.

        be-conductor attachment shape (from agent-view.html):
            {name: str, type: str (mime), data: str (base64)}

        ACP ContentBlock variants used:
          - image  — when the agent advertised promptCapabilities.image
          - resource — embedded text, when embeddedContext is supported
          - resource_link — a file path on disk (baseline support);
            used for binary blobs and as the fallback for text when
            embeddedContext is unavailable.
        """
        prompt_caps = self._agent_caps.get("promptCapabilities") or {}
        supports_image = bool(prompt_caps.get("image"))
        supports_embedded = bool(prompt_caps.get("embeddedContext"))

        blocks: list[dict] = []
        text_addendum: list[str] = []
        prompt_uid = _uuid.uuid4().hex[:8]

        for idx, att in enumerate(attachments):
            mime = att.get("type") or "application/octet-stream"
            name = att.get("name") or f"attachment-{idx}"
            data = att.get("data") or ""

            if mime.startswith("image/") and supports_image:
                blocks.append({
                    "type": "image",
                    "mimeType": mime,
                    "data": data,  # already base64
                })
                continue

            if mime.startswith("text/") or mime in (
                "application/json", "application/x-yaml", "application/xml",
            ):
                try:
                    decoded = base64.b64decode(data).decode(
                        "utf-8", errors="replace")
                except Exception:
                    decoded = "(failed to decode)"
                MAX = 64 * 1024
                if len(decoded) > MAX:
                    decoded = decoded[:MAX] + \
                        "\n\n[…truncated by be-conductor at 64 KB]"
                if supports_embedded:
                    blocks.append({
                        "type": "resource",
                        "resource": {
                            "uri": f"attachment://{name}",
                            "mimeType": mime,
                            "text": decoded,
                        },
                    })
                else:
                    text_addendum.append(
                        f"[Attached file: {name}]\n```\n{decoded}\n```")
                continue

            # Binary / unknown / image-without-vision — save to disk and
            # reference it. The agent can reach it via its read tool
            # (which routes through our fs/read_text_file handler).
            try:
                raw = base64.b64decode(data)
                tmp_dir = Path(tempfile.gettempdir()) / "be-conductor-uploads"
                tmp_dir.mkdir(exist_ok=True)
                safe_name = f"{prompt_uid}-{idx:02d}-{name}"
                tmp_path = tmp_dir / safe_name
                tmp_path.write_bytes(raw)
                blocks.append({
                    "type": "resource_link",
                    "uri": tmp_path.as_uri(),
                    "name": name,
                    "mimeType": mime,
                })
                text_addendum.append(
                    f"[Attached file: {name}] saved to: {tmp_path}")
            except Exception:
                text_addendum.append(
                    f"[Attached file: {name} — failed to save]")

        full_text = text
        if text_addendum:
            joined = "\n\n".join(text_addendum)
            full_text = (full_text + "\n\n" + joined).strip() \
                if full_text else joined
        # The text block goes last so the model reads attachments first.
        blocks.append({"type": "text", "text": full_text})
        return blocks

    async def _emit(self, event: AgentEvent) -> None:
        """Push an event to the orchestrator, stamping `btw` while a
        side-channel turn is active so it isn't persisted to history."""
        if self._btw_turn and "btw" not in event:
            event["btw"] = True
        await self._event_queue.put(event)

    async def _open_stream_block(self, block_type: str) -> None:
        """Ensure a streaming block of `block_type` ("text"/"thinking")
        is open, emitting stream_start. Closes any block of a different
        kind first. ACP has no explicit block boundaries, so we derive
        them from the chunk stream."""
        if self._open_block == block_type:
            return
        if self._open_block is not None:
            await self._close_stream_block()
        self._open_block = block_type
        await self._emit({"type": "stream_start", "block_type": block_type})

    async def _close_stream_block(self) -> None:
        """Emit stream_stop for the currently-open block, if any.

        Called at turn end so the frontend finalises the assistant
        bubble — without this the next turn's text streams into the
        previous turn's bubble."""
        if self._open_block is None:
            return
        bt = self._open_block
        self._open_block = None
        await self._emit({"type": "stream_stop", "block_type": bt})

    async def _emit_assistant_message(self) -> None:
        """Emit a complete assistant_message for the current turn.

        ACP only streams chunks; it has no final-message event. The
        orchestrator persists this snapshot to history, and history
        replay renders `assistant_message` (it skips the stream_*
        events) — so without this a reloaded ACP session shows every
        assistant answer blank. Content blocks match the shape the
        frontend's assistant_message renderer expects.
        """
        blocks: list[dict] = []
        if self._turn_thinking.strip():
            blocks.append({"type": "thinking",
                           "thinking": self._turn_thinking})
        if self._turn_text.strip():
            blocks.append({"type": "text", "text": self._turn_text})
        if not blocks:
            return
        await self._emit({
            "type": "assistant_message",
            "content": blocks,
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
        # ACP has no standard runtime model switch; MODEL_SWITCHING is
        # not advertised, so the orchestrator never calls this.
        raise NotImplementedError

    async def list_models(self) -> list[dict]:
        # ACP doesn't expose a model catalogue. The agent uses whatever
        # model its underlying CLI is configured for.
        return []

    async def set_agent(self, agent: str) -> None:
        raise NotImplementedError

    async def get_context_usage(self) -> dict:
        # ACP does not standardise token usage — the context ring is
        # hidden for ACP sessions (CONTEXT_USAGE not advertised).
        raise NotImplementedError

    async def respond_to_permission(
        self, request_id: str, decision: str,
    ) -> None:
        """Resolve a pending session/request_permission.

        `decision` is the ACP optionId the user picked. The orchestrator
        forwards the button choice verbatim; `_on_request` parked the
        JSON-RPC request on a Future which we now resolve so the reply
        is sent back to the agent.
        """
        pending = self._pending_permissions.get(request_id)
        if not pending:
            return
        fut: asyncio.Future = pending["future"]
        if not fut.done():
            option_ids = pending.get("option_ids", [])
            outcome: dict
            # Map common be-conductor button strings onto ACP option
            # kinds when the caller didn't pass a raw optionId.
            chosen = decision
            if decision not in option_ids:
                chosen = self._map_decision(decision, pending.get("options", []))
            if chosen:
                outcome = {"outcome": "selected", "optionId": chosen}
            else:
                outcome = {"outcome": "cancelled"}
            fut.set_result({"outcome": outcome})

    @staticmethod
    def _map_decision(decision: str, options: list[dict]) -> str | None:
        """Best-effort map of a free-text decision to an ACP optionId."""
        d = (decision or "").lower()
        want_kinds: list[str]
        if d in ("allow", "yes", "approve", "once", "allow-once"):
            want_kinds = ["allow_once", "allow_always"]
        elif d in ("always", "allow-always"):
            want_kinds = ["allow_always", "allow_once"]
        elif d in ("reject", "no", "deny", "reject-once"):
            want_kinds = ["reject_once", "reject_always"]
        else:
            want_kinds = []
        for kind in want_kinds:
            for opt in options:
                if opt.get("kind") == kind:
                    return opt.get("optionId")
        return options[0].get("optionId") if options else None

    # ----- inbound: notifications (agent → client) --------------------

    async def _on_disconnect(self) -> None:
        """The adapter process exited unexpectedly.

        Surface a clear error and end the session cleanly — without
        this, events() would hang on an empty queue and the UI would
        show a stuck session with no explanation.
        """
        if self._closed:
            return
        await self._emit({
            "type": "error",
            "error": "The ACP agent disconnected. Start a new session "
                     "(or Resume) to continue.",
            "subtype": "agent_disconnected",
        })
        await self._event_queue.put({"type": "session_end", "exit_code": 1})
        self._closed = True

    async def _on_notification(self, method: str, params: dict) -> None:
        """Handle a JSON-RPC notification from the adapter."""
        if method != "session/update":
            return
        if params.get("sessionId") not in (None, self._session_id):
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")

        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content") or {}
            text = content.get("text", "") if isinstance(content, dict) else ""
            if not text:
                return
            is_thought = kind == "agent_thought_chunk"
            block_type = "thinking" if is_thought else "text"
            # Accumulate for the end-of-turn assistant_message snapshot.
            if is_thought:
                self._turn_thinking += text
            else:
                self._turn_text += text
            # Synthesise a stream_start the first time a block of this
            # kind streams (and close any block of the other kind).
            await self._open_stream_block(block_type)
            await self._emit({
                "type": "stream_delta",
                "delta_type": block_type,
                block_type: text,
            })
            return

        if kind == "user_message_chunk":
            # Echoed user input — the orchestrator emits its own
            # user_message, so we drop this to avoid duplication.
            return

        if kind == "tool_call":
            # A tool call interrupts text streaming — close the current
            # block so the tool widget renders between bubbles, and the
            # post-tool text gets its own fresh bubble.
            await self._close_stream_block()
            tc_id = update.get("toolCallId", "")
            rec = {
                "tool": update.get("title") or update.get("kind") or "tool",
                "kind": update.get("kind", "other"),
                "input": update.get("rawInput") or {},
            }
            self._tool_calls[tc_id] = rec
            await self._emit({
                "type": "tool_use_start",
                "tool": rec["tool"],
                "tool_use_id": tc_id,
                "input": _clean_tool_input(rec["input"]),
            })
            return

        if kind == "tool_call_update":
            tc_id = update.get("toolCallId", "")
            rec = self._tool_calls.setdefault(
                tc_id, {"tool": "tool", "kind": "other", "input": {}})
            if "rawInput" in update:
                rec["input"] = update["rawInput"] or {}
            status = update.get("status")
            output = self._extract_tool_content(update.get("content"))
            if not output:
                # `content` was empty — fall back to rawOutput, but it
                # may be a dict / list; coerce to a display string so
                # the frontend never renders "[object Object]".
                output = _stringify_tool_value(update.get("rawOutput"))
            # Show a clean, human-readable input — never the adapter's
            # internal bookkeeping (call_id, process_id, parsed_cmd…).
            disp_input = _clean_tool_input(rec["input"])
            if status in ("completed", "failed"):
                await self._emit({
                    "type": "tool_use_end",
                    "tool": rec["tool"],
                    "tool_use_id": tc_id,
                    "input": disp_input,
                    "output": output,
                    "is_error": status == "failed",
                })
                self._tool_calls.pop(tc_id, None)
            else:
                await self._emit({
                    "type": "tool_use_progress",
                    "tool": rec["tool"],
                    "tool_use_id": tc_id,
                    "input": disp_input,
                    "output": output,
                })
            return

        if kind == "plan":
            await self._emit({
                "type": "system",
                "subtype": "plan",
                "payload": {"entries": update.get("entries") or []},
            })
            return

        if kind == "available_commands_update":
            self._available_commands = update.get("availableCommands") or []
            await self._emit({
                "type": "system",
                "subtype": "commands",
                "payload": {"commands": self._available_commands},
            })
            return

        if kind == "current_mode_update":
            await self._emit({
                "type": "system",
                "subtype": "mode",
                "payload": {"modeId": update.get("currentModeId")},
            })
            return

        if kind == "usage_update":
            # Some adapters (Codex) report context-window usage as
            # {used, size}. Surface it as a context_usage event so the
            # dashboard's context ring works for ACP sessions too.
            used = update.get("used")
            size = update.get("size")
            try:
                used_i = int(used) if used is not None else None
                size_i = int(size) if size is not None else None
            except (TypeError, ValueError):
                used_i = size_i = None
            if used_i is not None and size_i and size_i > 0:
                # Keys match what agent-view.html's context_usage
                # handler reads: totalTokens / maxTokens / percentage.
                await self._emit({
                    "type": "context_usage",
                    "data": {
                        "totalTokens": used_i,
                        "maxTokens": size_i,
                        "percentage": round(100.0 * used_i / size_i, 2),
                    },
                })
            return

        # Some adapters report a turn-level failure as an error-shaped
        # update rather than a JSON-RPC error response. Catch the common
        # shapes so the user sees a real message, never an empty bubble.
        if kind in ("error", "session_error") or update.get("error"):
            msg = _extract_error_text(update.get("error") or update)
            await self._emit({
                "type": "error",
                "error": msg,
                "subtype": "provider_error",
            })
            return

        # Unknown sessionUpdate variant — ignore silently for
        # forward-compatibility with newer ACP versions.

    @staticmethod
    def _extract_tool_content(content: Any) -> str:
        """Flatten an ACP tool-call `content` array to display text."""
        if not content:
            return ""
        if isinstance(content, str):
            return content
        out: list[str] = []
        for item in content if isinstance(content, list) else [content]:
            if not isinstance(item, dict):
                out.append(str(item))
                continue
            # {type: "content", content: {type: "text", text: "…"}}
            inner = item.get("content")
            if isinstance(inner, dict):
                if inner.get("type") == "text":
                    out.append(inner.get("text", ""))
                elif inner.get("type") == "resource":
                    res = inner.get("resource") or {}
                    out.append(res.get("text", ""))
            elif item.get("type") == "text":
                out.append(item.get("text", ""))
            elif item.get("type") == "diff":
                # ACP diff content — show a compact marker.
                path = item.get("path", "")
                out.append(f"[diff: {path}]")
        return "\n".join(s for s in out if s)

    # ----- inbound: requests (agent → client) -------------------------

    async def _on_request(self, method: str, params: dict) -> Any:
        """Handle a JSON-RPC request from the adapter.

        ACP agents call back to us for file I/O, terminals and
        permission. We execute file/terminal work ourselves so worktree
        isolation and cwd-scoping match the Claude path.
        """
        if method == "fs/read_text_file":
            return await self._handle_fs_read(params)
        if method == "fs/write_text_file":
            return await self._handle_fs_write(params)
        if method == "session/request_permission":
            return await self._handle_permission(params)
        if method == "terminal/create":
            return await self._handle_terminal_create(params)
        if method == "terminal/output":
            return self._handle_terminal_output(params)
        if method == "terminal/wait_for_exit":
            return await self._handle_terminal_wait(params)
        if method == "terminal/kill":
            return self._handle_terminal_kill(params)
        if method == "terminal/release":
            return self._handle_terminal_release(params)
        raise RuntimeError(f"unsupported ACP method: {method}")

    # --- file I/O (scoped to the session cwd) -------------------------

    def _resolve_in_cwd(self, path: str) -> Path:
        """Resolve `path` and refuse anything outside the session cwd."""
        base = Path(self._cwd).resolve()
        p = Path(path)
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        if base != p and base not in p.parents:
            raise RuntimeError(
                f"path escapes session directory: {path}")
        return p

    async def _handle_fs_read(self, params: dict) -> dict:
        path = self._resolve_in_cwd(params.get("path", ""))
        line = params.get("line")
        limit = params.get("limit")
        loop = asyncio.get_running_loop()

        def _read() -> str:
            text = path.read_text(encoding="utf-8", errors="replace")
            if line is None and limit is None:
                return text
            lines = text.splitlines(keepends=True)
            start = (line - 1) if line else 0
            end = (start + limit) if limit else len(lines)
            return "".join(lines[start:end])

        return {"content": await loop.run_in_executor(None, _read)}

    async def _handle_fs_write(self, params: dict) -> dict:
        path = self._resolve_in_cwd(params.get("path", ""))
        content = params.get("content", "")
        loop = asyncio.get_running_loop()

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await loop.run_in_executor(None, _write)
        return {}

    # --- permission ---------------------------------------------------

    async def _handle_permission(self, params: dict) -> dict:
        """Park a permission request and emit it to the UI.

        The agent's JSON-RPC request blocks until respond_to_permission()
        resolves the Future with the user's choice.
        """
        tool_call = params.get("toolCall") or {}
        tc_id = tool_call.get("toolCallId", "")
        options = params.get("options") or []
        # Use the toolCallId as the request id the UI replies against;
        # it is unique per in-flight tool call.
        request_id = tc_id or _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_permissions[request_id] = {
            "future": fut,
            "options": options,
            "option_ids": [o.get("optionId") for o in options],
        }
        rec = self._tool_calls.get(tc_id, {})
        await self._emit({
            "type": "permission_request",
            "request_id": request_id,
            "tool": rec.get("tool", tool_call.get("title", "")),
            "input": rec.get("input", {}),
            "payload": {
                "options": options,
                "toolCall": tool_call,
            },
        })
        try:
            result = await fut
        finally:
            self._pending_permissions.pop(request_id, None)
        # Tell the UI the request was resolved.
        decided = (result.get("outcome") or {}).get("optionId", "")
        await self._emit({
            "type": "permission_resolved",
            "request_id": request_id,
            "decision": decided or "cancelled",
        })
        return result

    # --- terminals ----------------------------------------------------
    #
    # ACP terminals let an agent run a long-lived command and poll its
    # output. We back each with a real subprocess scoped to the session
    # cwd. Terminal ids are local to this provider instance.

    async def _handle_terminal_create(self, params: dict) -> dict:
        if not hasattr(self, "_terminals"):
            self._terminals: dict[str, dict] = {}
        command = params.get("command", "")
        args = params.get("args") or []
        cwd = params.get("cwd") or self._cwd
        env_list = params.get("env") or []
        env = dict(os.environ)
        for item in env_list:
            if isinstance(item, dict) and "name" in item:
                env[item["name"]] = item.get("value", "")
        term_id = "term-" + _uuid.uuid4().hex[:12]
        proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        rec = {"proc": proc, "output": bytearray(), "drain": None}

        async def _drain() -> None:
            assert proc.stdout is not None
            with suppress(Exception):
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    rec["output"].extend(chunk)

        rec["drain"] = asyncio.create_task(_drain())
        self._terminals[term_id] = rec
        return {"terminalId": term_id}

    def _handle_terminal_output(self, params: dict) -> dict:
        rec = getattr(self, "_terminals", {}).get(params.get("terminalId", ""))
        if rec is None:
            return {"output": "", "truncated": False}
        proc = rec["proc"]
        result: dict[str, Any] = {
            "output": bytes(rec["output"]).decode("utf-8", errors="replace"),
            "truncated": False,
        }
        if proc.returncode is not None:
            result["exitStatus"] = {"exitCode": proc.returncode}
        return result

    async def _handle_terminal_wait(self, params: dict) -> dict:
        rec = getattr(self, "_terminals", {}).get(params.get("terminalId", ""))
        if rec is None:
            return {"exitCode": None}
        proc = rec["proc"]
        await proc.wait()
        if rec["drain"] is not None:
            with suppress(Exception):
                await rec["drain"]
        return {"exitCode": proc.returncode}

    def _handle_terminal_kill(self, params: dict) -> dict:
        rec = getattr(self, "_terminals", {}).get(params.get("terminalId", ""))
        if rec is not None and rec["proc"].returncode is None:
            with suppress(ProcessLookupError, Exception):
                rec["proc"].kill()
        return {}

    def _handle_terminal_release(self, params: dict) -> dict:
        rec = getattr(self, "_terminals", {}).pop(params.get("terminalId", ""), None)
        if rec is not None:
            if rec["proc"].returncode is None:
                with suppress(ProcessLookupError, Exception):
                    rec["proc"].kill()
            if rec["drain"] is not None:
                rec["drain"].cancel()
        return {}


def _bc_version() -> str:
    """be-conductor version, for the ACP clientInfo block."""
    try:
        from importlib.metadata import version
        return version("be-conductor")
    except Exception:
        return "0.0.0"


def _stringify_tool_value(value: Any) -> str:
    """Coerce an arbitrary ACP tool output value to a display string.

    `rawOutput` may be a string, a dict, a list, or None. Passing a
    dict straight through makes the frontend render "[object Object]";
    this always returns a string.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Common shapes: {output: "..."} / {text: "..."} / {content: ...}
        for key in ("output", "text", "content", "stdout", "message"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v
        try:
            return json.dumps(value, indent=2)
        except Exception:
            return str(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_stringify_tool_value(v) for v in value)
    return str(value)


# Keys in an ACP adapter's rawInput that are internal bookkeeping, not
# anything a user wants to see (Codex fills rawInput with these).
_TOOL_INPUT_NOISE = {
    "call_id", "process_id", "turn_id", "started_at_ms", "source",
    "parsed_cmd", "session_id", "sessionId",
}


def _clean_tool_input(raw: Any) -> dict:
    """Reduce an ACP tool input to the fields worth displaying.

    Codex's rawInput carries a lot of internal bookkeeping (call_id,
    process_id, parsed_cmd, …). Strip those; keep meaningful fields
    like `command`, `cwd`, `path`. Returns {} when nothing useful
    remains (the frontend then shows no input blob at all).
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _TOOL_INPUT_NOISE:
            continue
        # A `command` array like ["/bin/bash","-lc","ls -l x"] reads
        # better joined into a single string.
        if k == "command" and isinstance(v, (list, tuple)):
            cleaned[k] = " ".join(str(x) for x in v)
        else:
            cleaned[k] = v
    return cleaned


def _extract_error_text(err: Any) -> str:
    """Best-effort human-readable message from an ACP error payload.

    ACP errors arrive in several shapes — a JSON-RPC error object
    ({code, message, data}), an adapter-specific nested error
    ({name, data:{message}}), or a bare string. Some adapters wrap a
    provider error (e.g. Codex's ProviderAuthError) under a generic
    name. This walks the common shapes and always returns a non-empty
    string, so the user never sees an empty "Unknown error" bubble.
    """
    if err is None:
        return "The agent reported an error (no detail provided)."
    if isinstance(err, str):
        return err.strip() or "The agent reported an error."
    if not isinstance(err, dict):
        return str(err)

    # Collect candidate message fields from this level and one nesting
    # level down (data / error are the usual nests).
    parts: list[str] = []
    name = err.get("name") or err.get("code")
    msg = err.get("message")
    data = err.get("data")
    if isinstance(data, dict):
        msg = msg or data.get("message") or data.get("detail")
        # ProviderAuthError-style: data.message often has the real text.
        if not msg:
            for v in data.values():
                if isinstance(v, str) and v.strip():
                    msg = v
                    break
    inner = err.get("error")
    if not msg and inner is not None:
        return _extract_error_text(inner)

    if name and name not in ("APIError", "Error"):
        parts.append(str(name))
    if msg:
        parts.append(str(msg))
    text = ": ".join(p for p in parts if p).strip()
    if text:
        return text
    # Last resort — show the raw payload rather than an empty string.
    try:
        return f"The agent reported an error: {json.dumps(err)[:300]}"
    except Exception:
        return "The agent reported an error (unrecognised payload)."
