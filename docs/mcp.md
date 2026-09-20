# MCP — drive be-conductor from Claude Desktop

be-conductor exposes an [MCP](https://modelcontextprotocol.io) server so
Claude Desktop (or any MCP client) can delegate work to your other agents and
accounts: *"have Codex review this"*, *"run that on the Max5 subscription"*.

Two transports, one tool set:

- **Streamable HTTP** at `http://<host>:7777/mcp` on the normal server.
  Honours the bearer token (`BE_CONDUCTOR_TOKEN`) like every other route, and
  works across machines over Tailscale.
- **stdio bridge** — `be-conductor mcp` — for clients that only launch local
  servers, such as Claude Desktop. It forwards to `/mcp`, and if the local
  server is down it starts it first. Only protocol messages go to stdout.

## Setup

1. Enable it and say where MCP-started sessions may work — Settings → **MCP**
   in the dashboard, or in `~/.be-conductor/config.yaml`:

   ```yaml
   mcp:
     enabled: true
     allowed_dirs: ["~/code"]            # required: working_dir must be inside one of these
     expose_commands: ["Claude Code — Max5", "Codex — Pro5"]   # labels; empty = every command with a headless block
     default_timeout_seconds: 900
   ```

2. Register the bridge:

   ```bash
   be-conductor install-mcp
   ```

   Claude has **two separate MCP lists**, and this registers in whichever of
   them exists on the machine:

   | Target | Used by | Where |
   |---|---|---|
   | `desktop` | Claude Desktop's chat side | `claude_desktop_config.json` (`~/.config/Claude/`, `~/Library/Application Support/Claude/`, `%APPDATA%\Claude\`) — backed up first |
   | `code` | Claude Code: the CLI **and the Code tab of Claude Desktop** | user scope in `~/.claude.json`, written through `claude mcp add` (that file is live, so it is never edited directly) |

   `--target desktop|code|both` limits it (default `both`).
   `be-conductor uninstall-mcp` removes it again; both are also buttons in
   Settings → MCP, which shows the status of each target. Restart Claude
   Desktop, or start a new Claude Code session, afterwards.

   For any other MCP client:

   ```json
   { "mcpServers": { "be-conductor": { "command": "/abs/path/to/be-conductor", "args": ["mcp"] } } }
   ```

Toggling `enabled` takes effect immediately; no restart is needed. While it is
off, `/mcp` answers 404.

## Tools

| Tool | |
|---|---|
| `run_<label>(prompt, working_dir, model?, continue_session?, worktree=false, wait=true, timeout_seconds?)` | One per exposed headless command, e.g. `run_claude_code_max5`. Runs the prompt to completion and returns the agent's answer with a footer: `[session=… profile=… model=… status=… duration=…s cost=$… dashboard=…]`. `model` picks the model for this one run (the agent's `--model` flag); without it the profile's model, else the agent's default, is used. `wait=false` returns the session name at once. |
| `get_result(session)` | Status + result; while still running, the tail of the output. |
| `start_session(command_label, cwd, name?, profile?, worktree=false)` | Interactive session you drive with the next two tools — and can open in the dashboard. |
| `send_input(session, text?, keys?)` | `keys` e.g. `["ENTER"]`, `["CTRL+C"]`, `["UP"]`. |
| `read_output(session, lines=100)` | Plain-text tail of the terminal. |
| `list_sessions()` / `stop_session(session)` | |
| `list_profiles()` | Profiles, key status, today's spend, and the `run_*` tools. |
| `list_models(agent, contains?)` | Model ids that agent accepts, to pass as its `model`. `contains` filters — an OpenRouter key reaches hundreds. |
| `list_worktrees()` / `merge_worktree(name, strategy="squash")` / `discard_worktree(name)` | Review and land what a `worktree=true` run produced. |

The `run_*` tools follow `allowed_commands` live — edit a label or add a
headless block and the tool list changes without a restart.

**You conduct.** Nothing is delegated unless you ask for it: the tool
descriptions tell the calling model to use a `run_*` tool only when you named
that agent or account for the task, to pass `model` only when you named one,
and never to swap in a different agent or chain further ones on its own. "Make
a plan with run_claude_code_max5 on claude-fable-5-1 and show it to me", then
"hand that plan to run_codex_pro5" — each step is your call.

**Continuing a task.** A headless run is one process, but the agent's
conversation outlives it. Pass `continue_session=<session name of a finished
run>` and the next run resumes that thread with its context — Claude via
`--resume`, Codex via `codex exec resume`, OpenCode via `--session`. A run
that can be carried on says `continuable` in its footer. Use it for the steps
of one job; omit it for unrelated work. To keep a *process* alive instead, use
`start_session`.

**A question comes back to you.** If a run stops to ask something, the tool
returns straight away with the question rather than waiting out its timeout.
Answer with `send_input`, then `read_output`. This needs a terminal, so it does
not apply to commands marked `tty: false` (OpenCode).

**Headless means no questions asked of the agent.** The agent cannot ask the caller anything, so
prompts must be self-contained. If a run does stop at a prompt, its status
becomes `needs_input`, the usual notification fires (Telegram/Slack/browser),
and you can answer from the dashboard or with `send_input`; `get_result`
returns the finished answer later.

**Long runs.** An MCP client may give up on a tool call before the agent is
done. For anything over ~5 minutes use `wait=false` and poll `get_result`. The
run itself is unaffected by a client timeout or disconnect — it keeps going as
a normal session.

## Example prompts for Claude Desktop

- "Use run_codex_pro5 in ~/code/myapp to find why `test_sync` is flaky."
- "Start the refactor with run_claude_code_max5, worktree=true, wait=false —
  then check on it with get_result."
- "list_worktrees, then squash-merge the one from that run."

## Remote servers

Point the bridge at another machine's be-conductor:

```json
{ "mcpServers": { "workstation": { "command": "/abs/path/to/be-conductor",
    "args": ["mcp", "--server", "http://workstation.tailnet.ts.net:7777"],
    "env": { "BE_CONDUCTOR_TOKEN": "…" } } } }
```

Or keep one bridge and name the other machines in `config.yaml` — every tool
then gets an optional `server` argument (`"local"` by default):

```yaml
mcp:
  servers:
    - name: workstation
      url: http://workstation.tailnet.ts.net:7777
      token_env: WORKSTATION_BC_TOKEN      # or  token: "…"
```

"run_codex_pro5 on server workstation in ~/code/myapp: …" is then forwarded to
that machine's `/mcp` (its own profiles, commands and `allowed_dirs` apply).
The dashboard's server list lives in the browser, so the bridge cannot read
it — this list is separate.

Clients that speak streamable HTTP can skip the bridge and use
`http://<host>:7777/mcp` directly.

## Security notes

- `working_dir` / `cwd` outside `mcp.allowed_dirs` is refused; with no
  allowed dirs configured every run is refused.
- MCP settings and `install-mcp` follow the admin rule of the rest of the
  dashboard: localhost, or a request carrying the token.
- Without a token, `/mcp` is as open as the rest of the server — the network
  (Tailscale) is the trust boundary. Set a token before exposing it wider.
