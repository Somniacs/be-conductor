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

"""MCP surface — lets an MCP client start, run, watch and answer sessions.

Served as streamable HTTP at ``/mcp`` on the main server (stateless, so a
server restart never strands a client).  ``be-conductor mcp`` bridges it to
stdio for clients that only launch local servers, e.g. Claude Desktop.
"""

import asyncio
import logging
import time
from urllib.parse import quote

# mcp 2.x renamed FastMCP to MCPServer and moved the transport options from
# the constructor to streamable_http_app(); 1.x is still what many installs
# carry.  Try 2.x first — under 2.x the old module is a stub that raises.
try:
    from mcp.server.mcpserver import Context, MCPServer as _MCPBase
    _MCP_V2 = True
except ImportError:
    from mcp.server.fastmcp import Context, FastMCP as _MCPBase
    _MCP_V2 = False
from mcp.server.transport_security import TransportSecuritySettings

from be_conductor.profiles import ProfileError, ledger, list_profiles, secrets
from be_conductor.sessions import headless as hl
from be_conductor.sessions import tasks
from be_conductor.utils import config as cfg

log = logging.getLogger(__name__)

_HEADLESS_NOTE = (
    "Headless: the agent cannot ask you questions; make the prompt "
    "self-contained. If it stalls you will be notified and can answer from "
    "the dashboard."
)
_CONDUCTOR_NOTE = (
    "The user conducts: call this only when the user asked for this agent / "
    "account for the task at hand. Never delegate on your own initiative, and "
    "never swap in a different agent than the one the user named."
)
_LONG_RUN_NOTE = (
    "For work likely to take more than ~5 minutes pass wait=false and poll "
    "get_result with the returned session name — the MCP client may give up "
    "on a long tool call before the agent is done."
)

_last_request: float | None = None   # when an MCP client last talked to us


def bridge_status() -> dict:
    return {"last_request": _last_request}


def _registry():
    from be_conductor.api.routes import registry
    return registry


def _dashboard_link(session_name: str) -> str:
    from be_conductor.api.routes import _get_dashboard_base_url
    return f"{_get_dashboard_base_url()}#session={quote(session_name)}"


def exposed_entries() -> list[dict]:
    """The allowed_commands entries that get a run_<slug> tool."""
    wanted = [str(x) for x in (cfg.MCP_CONFIG.get("expose_commands") or [])]
    out = []
    for e in cfg.ALLOWED_COMMANDS:
        if not isinstance(e, dict) or not e.get("command"):
            continue
        label = e.get("label") or e["command"]
        if wanted:
            if label in wanted and hl.headless_block(e):
                out.append(e)
        elif hl.is_exposed_headless(e):
            out.append(e)
    return out


def _find_session_meta(session: str) -> dict | None:
    reg = _registry()
    live = reg.get(session)
    if live is not None:
        return live.to_dict()
    if session in reg.resumable:
        return reg.resumable[session]
    for meta in reg.resumable.values():
        if meta.get("name") == session:
            return meta
    return None


def _format_result(record: dict, tail: str | None = None) -> str:
    footer = tasks.format_footer(record, _dashboard_link(record.get("name") or record["id"]))
    status = record.get("task_status")
    if status in ("running", "needs_input"):
        head = ("The run is WAITING FOR INPUT — answer it from the dashboard or with send_input."
                if status == "needs_input" else "The run is still in progress.")
        body = f"{head}\n\n--- output so far ---\n{tail or '(no output yet)'}"
    else:
        body = record.get("result") or "(the agent produced no output)"
        if status == "timeout":
            body = "The run hit its timeout and was stopped. Partial output:\n\n" + body
        elif status == "failed":
            body = f"The run failed ({record.get('fail_reason') or 'error'}).\n\n" + body
    return f"{body}\n\n{footer}"


class ConductorMCP(_MCPBase):
    """MCP server whose run_* tools follow allowed_commands as it changes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run_tools: set[str] = set()
        self._run_tools_version: int | None = None

    def _drop_tool(self, name: str):
        remove = getattr(self, "remove_tool", None) or self._tool_manager.remove_tool
        try:
            remove(name)
        except Exception:
            pass

    def sync_run_tools(self):
        """Rebuild the run_<label> tools if the config changed. Called per
        HTTP request, so it must stay a cheap version compare."""
        version = cfg.get_config_version()
        if version == self._run_tools_version:
            return
        for name in self._run_tools:
            self._drop_tool(name)
        self._run_tools = set()
        for entry in exposed_entries():
            label = entry.get("label") or entry["command"]
            name = f"run_{tasks.slugify(label)}"
            if name in self._run_tools:
                continue   # two labels with the same slug — first one wins
            self.add_tool(_make_run_tool(entry), name=name,
                          description=_run_tool_description(entry))
            self._run_tools.add(name)
        self._run_tools_version = version


def _touch():
    global _last_request
    _last_request = time.time()


def _run_tool_description(entry: dict) -> str:
    label = entry.get("label") or entry["command"]
    parts = [f"Run a task with {label} and return the agent's final answer."]
    pname = entry.get("profile")
    if pname:
        desc = next((p.get("description") for p in list_profiles() if p["name"] == pname), None)
        parts.append(desc or f"Runs under the '{pname}' account profile.")
    parts.append(_CONDUCTOR_NOTE)
    parts.append(_HEADLESS_NOTE)
    parts.append(
        "model is optional: pass it only when the user names a model for this "
        "run (it goes to the agent's --model flag, e.g. a Claude model id or "
        "alias for Claude Code, a GPT model id for Codex, provider/model for "
        "OpenCode); omit it to use the account's default. "
        "working_dir must be inside the server's allowed directories. "
        "worktree=true runs in an isolated git worktree (review it with "
        "list_worktrees / merge_worktree). wait=true blocks until the run is "
        "done or timeout_seconds passes (then partial output and the session "
        "name are returned); wait=false returns the session name immediately. "
        + _LONG_RUN_NOTE)
    return " ".join(parts)


def _make_run_tool(entry: dict):
    label = entry.get("label") or entry["command"]

    async def run(prompt: str, working_dir: str, model: str | None = None,
                  worktree: bool = False, wait: bool = True,
                  timeout_seconds: int | None = None,
                  ctx: Context | None = None) -> str:
        try:
            cwd = tasks.check_allowed_dir(working_dir)
            session = await tasks.start_task(
                _registry(), label, prompt, cwd=cwd, worktree=worktree,
                timeout_seconds=timeout_seconds, model=model)
        except (PermissionError, ValueError, FileNotFoundError) as e:
            return f"Refused: {e}"

        if not wait:
            return (f"Started. Poll get_result(session=\"{session.name}\") for the answer.\n\n"
                    + tasks.format_footer(session.task_record(), _dashboard_link(session.name)))

        # The run enforces its own timeout; the margin only covers teardown.
        deadline = time.monotonic() + (session.timeout_seconds or 900) + 20
        while not await session.wait(timeout=10):
            if time.monotonic() > deadline:
                break
            if ctx is not None:
                try:   # keep-alive for clients that reset their timer on progress
                    await ctx.report_progress(
                        progress=session.duration_s or 0,
                        message=f"{label}: {session.task_status}")
                except Exception:
                    pass
        tail = session.get_buffer_text(max_lines=60)
        return _format_result(session.task_record(), tail)

    return run


# Stateless: every request stands alone, so a server restart never strands a
# client.  The server is reached by LAN/Tailscale names too, and access
# control is be-conductor's own bearer token — not the Host header.
_TRANSPORT_OPTIONS = dict(
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def create_mcp() -> ConductorMCP:
    mcp = ConductorMCP(
        "be-conductor",
        instructions=(
            "be-conductor runs coding agents (Claude Code, Codex, OpenCode, …) as "
            "managed sessions, optionally under separate account profiles. The user "
            "is the conductor: they decide which agent, account and model gets "
            "which task. Use a run_* tool only when the user asks for that agent or "
            "account, pass `model` only when they name one, and bring the result "
            "back to them — do not delegate, pick an agent, or chain further agents "
            "on your own. start_session / send_input / read_output drive an "
            "interactive session, under the same rule."),
        **({} if _MCP_V2 else _TRANSPORT_OPTIONS),
    )

    @mcp.tool()
    async def get_result(session: str) -> str:
        """Status and result of a run started with a run_* tool. While it is
        still running, returns the tail of its output instead."""
        record = _registry().task_result(session)
        if record is None:
            return f"No headless run named '{session}'."
        live = _registry().get(session)
        tail = live.get_buffer_text(max_lines=60) if live is not None else None
        return _format_result(record, tail)

    @mcp.tool()
    async def start_session(command_label: str, cwd: str, name: str | None = None,
                            profile: str | None = None, worktree: bool = False) -> str:
        """Start an interactive agent session (a terminal you drive with
        send_input / read_output, and the user can open in the dashboard).
        Only when the user asked for this agent / account.
        command_label is a label from list_profiles' command list or
        allowed_commands; profile overrides the command's own profile."""
        try:
            entry = tasks.resolve_entry(command_label, profile)
            workdir = tasks.check_allowed_dir(cwd)
            session = await _registry().create(
                name or tasks.task_name(entry.get("label") or entry["command"]).replace("task-", "mcp-", 1),
                entry["command"], cwd=workdir, worktree=worktree,
                profile=profile or entry.get("profile") or None)
        except (PermissionError, ValueError, FileNotFoundError) as e:
            return f"Refused: {e}"
        return (f"Session '{session.name}' started ({entry['command']}"
                f"{', profile ' + session.profile if session.profile else ''}) in {session.cwd}.\n"
                f"dashboard={_dashboard_link(session.name)}")

    @mcp.tool()
    async def send_input(session: str, text: str | None = None,
                         keys: list[str] | None = None) -> str:
        """Type into a session. text is sent as-is (no Enter); keys are named
        keys sent after it, e.g. ["ENTER"], ["CTRL+C"], ["UP"], ["ESCAPE"]."""
        from be_conductor.api.routes import _KEY_MAP
        live = _registry().get(session)
        if live is None or not hasattr(live, "send_input"):
            return f"No running terminal session '{session}'."
        unknown = [k for k in (keys or []) if k.upper() not in _KEY_MAP]
        if unknown:
            return f"Unknown keys {unknown}. Known: {', '.join(_KEY_MAP)}"
        if text:
            live.send_input(text)
        for k in keys or []:
            if text:
                await asyncio.sleep(0.05)   # let the TUI take the text first
            live.send_input(_KEY_MAP[k.upper()])
        return "sent"

    @mcp.tool()
    async def read_output(session: str, lines: int = 100) -> str:
        """The last lines of a session's terminal output (plain text)."""
        live = _registry().get(session)
        if live is None:
            meta = _find_session_meta(session)
            if meta and meta.get("headless"):
                return "Session has exited — use get_result."
            return f"No running session '{session}'."
        if not hasattr(live, "get_buffer_text"):
            return "This is an Agent SDK session — it has no terminal output."
        return live.get_buffer_text(max_lines=max(1, min(lines, 2000))) or "(no output yet)"

    @mcp.tool()
    async def list_sessions() -> str:
        """All sessions on this server: running ones and finished/resumable ones."""
        rows = []
        for s in _registry().list_all():
            bits = [s.get("name", "?"), s.get("status", "?")]
            if s.get("headless"):
                bits.append(f"task={s.get('task_status')}")
            if s.get("profile"):
                bits.append(f"profile={s['profile']}")
            bits.append(f"cmd={s.get('command', '')[:40]}")
            if s.get("cwd"):
                bits.append(f"cwd={s['cwd']}")
            rows.append("  ".join(bits))
        return "\n".join(rows) or "(no sessions)"

    @mcp.tool()
    async def stop_session(session: str) -> str:
        """Stop a running session (kills its process tree)."""
        live = _registry().get(session)
        if live is None:
            return f"No running session '{session}'."
        await _registry().remove(live.id)
        return f"Session '{session}' stopped."

    @mcp.tool(name="list_profiles")
    async def list_profiles_tool() -> str:
        """Account profiles (separate agent logins / API keys) with today's
        spend, and the commands that can be run headless."""
        lines = ["Profiles:"]
        for p in list_profiles():
            keys = ", ".join(
                f"{s['env']}={'set' if secrets.has_secret(s['keyring']) else 'MISSING'}"
                for s in p["secrets"]) or "login-based"
            today = ledger.usage(p["name"])["today"]
            cap = f", cap ${p['max_cost_usd_per_run']}/run" if p.get("max_cost_usd_per_run") else ""
            lines.append(f"  {p['name']}  backend={p['backend']}  keys: {keys}  "
                         f"today: {today['runs']} runs, ${today['cost_usd']:.2f}{cap}"
                         + (f"  — {p['description']}" if p.get("description") else ""))
        if len(lines) == 1:
            lines.append("  (none configured)")
        lines.append("Headless commands:")
        for e in exposed_entries():
            label = e.get("label") or e["command"]
            lines.append(f"  run_{tasks.slugify(label)}  = {label}"
                         + (f"  (profile {e['profile']})" if e.get("profile") else ""))
        return "\n".join(lines)

    # The worktree tools call the REST handlers, so a merge from an MCP
    # client does exactly what the dashboard's merge button does.

    @mcp.tool()
    async def list_worktrees() -> str:
        """Git worktrees created for sessions, with their review state."""
        from be_conductor.api import routes
        data = await routes.list_worktrees()
        items = data.get("worktrees", data) if isinstance(data, dict) else data
        rows = [f"{d.get('name')}  status={d.get('status')}  branch={d.get('branch')}  "
                f"ahead={d.get('commits_ahead', 0)}  repo={d.get('repo_path')}"
                for d in items or []]
        return "\n".join(rows) or "(no worktrees)"

    @mcp.tool()
    async def merge_worktree(name: str, strategy: str = "squash") -> str:
        """Merge a session's worktree back into its base branch. The session
        must be stopped. strategy: squash | merge | rebase."""
        from fastapi import HTTPException
        from be_conductor.api import routes
        if strategy not in ("squash", "merge", "rebase"):
            return "strategy must be squash, merge or rebase"
        try:
            info = await routes.get_worktree(name)
            if info.get("status") == "active":
                await routes.finalize_worktree(name)
            r = await routes.merge_worktree(name, routes.MergeRequest(strategy=strategy))
        except HTTPException as e:
            return f"Merge failed: {e.detail}"
        if not r["success"]:
            conflicts = ", ".join(r.get("conflict_files") or [])
            return f"Merge failed: {r['message']}" + (f" (conflicts: {conflicts})" if conflicts else "")
        return (f"Merged {r['merged_branch']} into {r['target_branch']} "
                f"({r['strategy']}, {r['commits_merged']} commits). {r['message']}")

    @mcp.tool()
    async def discard_worktree(name: str) -> str:
        """Delete a session's worktree and its branch, discarding its changes."""
        from fastapi import HTTPException
        from be_conductor.api import routes
        try:
            await routes.delete_worktree(name, force=True)
        except HTTPException as e:
            return f"Discard failed: {e.detail}"
        return f"Worktree '{name}' discarded."

    return mcp


class _GatedMCPApp:
    """ASGI endpoint for /mcp that answers only while ``mcp.enabled`` is set.

    Checked per request, so the dashboard toggle works without a restart.
    """

    def __init__(self, mcp: ConductorMCP, inner):
        self._mcp = mcp
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if not cfg.MCP_CONFIG.get("enabled"):
            from starlette.responses import JSONResponse
            resp = JSONResponse(
                {"detail": "MCP is disabled — enable it in Settings → MCP "
                           "(mcp.enabled in ~/.be-conductor/config.yaml)"},
                status_code=404)
            await resp(scope, receive, send)
            return
        _touch()
        self._mcp.sync_run_tools()
        await self._inner(scope, receive, send)


def mount(app) -> ConductorMCP:
    """Add the /mcp route to the FastAPI app. Returns the MCP instance whose
    session manager the app lifespan must run."""
    from starlette.routing import Route

    mcp = create_mcp()
    sub_app = mcp.streamable_http_app(**(_TRANSPORT_OPTIONS if _MCP_V2 else {}))
    # A Route (not a Mount) hands the sub-app the untouched "/mcp" path,
    # which is the one path it serves.
    app.router.routes.append(Route("/mcp", endpoint=_GatedMCPApp(mcp, sub_app)))
    return mcp
