# ♭conductor

Orchestrate your AI coding agents across your own machines — local-first, cloud-optional.

Your best ideas don't always happen at your desk. With be-conductor, you can start an agent session from your phone and let it run on your workstation, GPU box, or dev machine. AI agents run for minutes — sometimes hours — and then stall on a single question. If you're not at that terminal, the session idles until you return.

be-conductor keeps them moving. Get notified on **Slack** or **Telegram** the moment an agent stalls or finishes — then jump in from your phone and respond. It runs entirely on your machines. No remote backend. No vendor dependency. It wraps terminal sessions in a lightweight server and exposes them through a web dashboard you can open from your phone, tablet, or laptop. Pair it with [Tailscale](https://tailscale.com/) and you get secure access to all your machines — no port forwarding, no VPN setup, just works.

**New here?** Check out the [Quick Start Guide](docs/intro.md) — install, run an agent, and set up phone access in 5 minutes.

---

**Contents:** [What It Looks Like](#what-it-looks-like) · [How It Works](#how-it-works) · [Features](#features) · [What You Can Run](#what-you-can-run) · [Prerequisites](#prerequisites) · [Install](#install) · [Usage](#usage) · [Is It Safe?](#is-it-safe) · [Dashboard](#dashboard) · [CLI Reference](#cli-reference) · [API](#api) · [Agent Integration](#agent-integration) · [Project Structure](#project-structure) · [Platform Support](#platform-support)

---

## What It Looks Like

| Desktop | Mobile | Mobile (keyboard) |
|---------|--------|--------------------|
| <a href="data/desktop-split.png"><img src="data/desktop-split.png" alt="Desktop — split view" width="420"></a> | <a href="data/mobile-split.jpg"><img src="data/mobile-split.jpg" alt="Mobile — split view" width="150"></a> | <a href="data/mobile-keyboard.jpg"><img src="data/mobile-keyboard.jpg" alt="Mobile — keyboard" width="150"></a> |

`<agent>` is the command to run. Some examples:

| Agent | Command |
|---|---|
| Claude Code | `be-conductor run claude research` |
| Gemini CLI | `be-conductor run gemini research` |
| OpenCode | `be-conductor run opencode backend` |
| Codex CLI | `be-conductor run codex backend` |
| Aider | `be-conductor run aider refactor` |
| Goose | `be-conductor run goose api` |
| GitHub Copilot | `be-conductor run copilot chat` |
| Amp | `be-conductor run amp feature` |
| Forge | `be-conductor run forge pair` |
| Custom (allowlisted) | `be-conductor run python3 train` |

```
Start agents                Leave your desk         Answer from anywhere
──────────────              ───────────────         ────────────────────
be-conductor run <agent> dev   Go to a meeting.        Open dashboard on phone.
be-conductor run <agent> test  Grab coffee.            See all sessions.
be-conductor run <agent> api   Sit on the couch.       Type a response. Done.
                                                    Agent keeps going.
```

You can also start new sessions directly from the web dashboard — pick an agent, name the session, and hit Run. No terminal needed. When you're back at your computer, attach to any running session from the terminal with `be-conductor attach <name>`.

Sessions survive disconnects. Close the browser, reopen it later — everything is still there. When an agent session exits with a resume token (e.g. Claude Code's `--resume`), be-conductor captures it and lets you resume the conversation later — even after a reboot.

## How It Works

```
  Machine A (workstation)          Machine B (GPU box)
  ───────────────────────          ────────────────────
  Terminal Process × N             Terminal Process × N
        │                                │
    PTY Wrapper                      PTY Wrapper
        │                                │
  be-conductor Server                 be-conductor Server
    0.0.0.0:7777                     0.0.0.0:7777
        │                                │
        └──────── Tailscale ─────────────┘
                      │
              Browser Dashboard
          (connects to both servers)
```

Each process runs in a PTY on your machine. Output goes into a rolling in-memory buffer. When a browser connects, it gets the full buffer first, then live output over WebSocket. The dashboard connects directly to each server — no proxy, no hub. Each server stays independent.

## Features

- **Run any terminal process** — AI coding agents, training jobs, builds, or any interactive command that runs in a terminal.
- **Web dashboard** — full terminal rendering in the browser with split view, color themes, font controls, and keyboard input.
- **Mobile-ready** — responsive layout with on-screen extra keys, touch scrolling, and auto-fullscreen when the keyboard opens.
- **Multi-machine** — connect to multiple be-conductor servers from one dashboard; sessions are grouped by machine.
- **Git worktree isolation** — run agents in isolated branches so parallel sessions never conflict with each other or your work.
- **Session resume** — exited sessions with resume tokens are saved and can be relaunched with one click or from the CLI.
- **External session discovery** — find and resume sessions started in IDEs or other terminals (Claude Code, Codex, Copilot, Gemini, Goose).
- **Notifications** — get alerted on Slack, Telegram, Discord, or custom webhooks when a session stalls or finishes.
- **File viewer** — browse project files, view text with line numbers, render Markdown, display images and SVGs, preview PDFs, and download files — all from the dashboard.
- **File upload** — drag-and-drop, paste, or tap to upload files into a session; insert the path or copy it to clipboard.
- **IDE plugins** — VS Code and JetBrains plugins for managing sessions without leaving your editor.
- **Secure by default** — runs entirely on your machines with no cloud backend; pair with Tailscale for encrypted remote access.

## What You Can Run

be-conductor works with any interactive terminal process. The dashboard ships with presets for common AI agents, but you can run anything from the CLI:

- **AI coding agents** — Claude Code, Gemini CLI, OpenCode, Codex CLI, GitHub Copilot CLI, Goose, Amp, Aider, Forge, Cursor Agent
- **Training jobs** — long-running GPU training with live output
- **Builds and test suites** — compilation, CI pipelines, test runs
- **Any terminal process** — if it runs in a terminal, be-conductor can manage it

### Adding commands to the allowlist

The dashboard can only launch commands from the allowlist. The CLI is unrestricted.

Open the hamburger menu → **Settings** → **Agents** tab. Add, edit, or remove commands and click **Save**. Changes take effect immediately on all connected clients — no restart needed. Each command has a label (shown in the UI) and optional resume/stop settings for agent-specific behavior.

The other Settings tabs — **General** (server info, auth token, limits), **Directories** (default paths), **Servers** (multi-machine management), and **Notifications** (browser/webhook alerts) — are also managed from here. Admin tabs (General, Agents, Directories) are visible on localhost or when `BE_CONDUCTOR_TOKEN` is set.

<details>
<summary>Alternative: edit the config file directly</summary>

Edit `~/.be-conductor/config.yaml` (created on first save from Settings):

```yaml
allowed_commands:
  - command: "claude"
    label: "Claude Code"
  - command: "python3"
    label: "Python"
```

Optional fields for advanced behavior:

```yaml
  - command: "my-agent"
    label: "My Agent"
    resume_pattern: "--resume\\s+(\\S+)"    # regex to capture resume token from output
    resume_flag: "--resume"                 # flag used when resuming (token-based)
    resume_command: "my-agent --continue"   # fixed command for resume (command-based)
    stop_sequence: ["\x03", "/exit", "\r"]  # graceful stop keystrokes
```

Use `resume_pattern` + `resume_flag` for agents that print a resume token on exit (e.g. Claude Code). Use `resume_command` for agents that manage their own session history (e.g. Gemini, OpenCode, Goose). Don't set both.

After editing the file, restart the server: `be-conductor restart`.

</details>

## Prerequisites

- **Python 3.10+** — check with `python3 --version` (or `py --version` on Windows)
- **Git** — to clone the repository
- **Tailscale** (optional, for remote access) — install on your workstation and your phone, tablet, or laptop. Sign in with the same account on all devices. See [tailscale.com](https://tailscale.com/)

## Install

### Linux / [macOS](docs/MACOS.md)

#### One-line install (recommended)

```bash
curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash
```

#### From source

```bash
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
./install.sh
```

If the command is not found after install, restart your terminal or run `source ~/.bashrc` (or `~/.zshrc`).

The installer checks for Python 3.10+, installs [pipx](https://pipx.pypa.io/) if needed, and offers to set up autostart (systemd on Linux, launchd on macOS).

### [Windows](docs/WINDOWS.md)

Requires Windows 10 Build 1809+ or Windows 11 (for ConPTY support).

#### One-line install (recommended)

```powershell
irm https://github.com/somniacs/be-conductor/releases/latest/download/install.ps1 | iex
```

#### From source

```powershell
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer checks for Python 3.10+, installs [pipx](https://pipx.pypa.io/) if needed, and offers to set up autostart via Task Scheduler.

<details>
<summary>Manual install (without install script)</summary>

**Linux / macOS:**
```bash
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Windows:**
```powershell
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

</details>

### Updating

The dashboard shows a notification when a new version is available. To update, run the one-liner again or `./install.sh` from a cloned repo. On Windows, run `install.ps1` again. Your settings (`~/.be-conductor/config.yaml`), sessions, and uploads are preserved — only the application code is replaced.

### Uninstall

**Linux / macOS:**
```bash
curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/uninstall.sh | bash
```

**Windows:**
```powershell
irm https://github.com/somniacs/be-conductor/releases/latest/download/uninstall.ps1 | iex
```

This stops the server, removes autostart configs, uninstalls the package, and asks whether to keep or remove your data.

## Usage

### Start sessions

```bash
# Start one session (server auto-starts in background)
be-conductor run <agent> research

# Start more
be-conductor run <agent> coding
be-conductor run <agent> review
```

Open the dashboard in your browser — locally at `http://127.0.0.1:7777`, or from any device on your Tailscale network at `http://100.x.x.x:7777` (your Tailscale IP).

Want the dashboard always available? See [Auto-Start on Boot](docs/autostart.md) for systemd, launchd, and Task Scheduler setup.

### Git worktree isolation

When AI agents write code, they change files in your working directory — which can conflict with your own uncommitted work or other running agents. be-conductor solves this with **git worktree isolation**: each session gets its own branch and working copy, so agents never step on each other or on your work. This works with any agent — Claude Code, Aider, Codex, Goose, Copilot, or any custom command.

```bash
# Start a session in an isolated worktree
be-conductor run -w claude refactor-auth

# Start another — both run in parallel on separate branches
be-conductor run -w claude add-tests
```

Each worktree session:
- Gets a fresh branch based on your current HEAD
- Runs in its own directory (under `.be-conductor-worktrees/` in the repo)
- Auto-commits changes when the session stops or before a merge
- Shows branch name and commit count in the dashboard sidebar

The worktree lifecycle is simple: **work → merge → repeat → delete**.

```bash
# See all worktrees and their status
be-conductor worktree list

# Merge changes back (squash, merge, or rebase)
be-conductor worktree merge refactor-auth --strategy squash

# Or discard if you don't want the changes
be-conductor worktree discard add-tests

# Clean up stale worktrees
be-conductor worktree gc
```

**Merging is non-destructive.** When you merge, the worktree stays alive. You can resume the session, make more changes, and merge again — as many times as needed. The merge button only appears when there are new commits to merge. When you're fully done, delete the worktree with the × button.

You can also manage worktrees directly from the dashboard. The sidebar shows a git-branch icon for worktree sessions with the branch name and commit count. Stopped worktree sessions show three buttons:

- **▶ Resume** — reopen the session and keep working
- **↻ Merge** — opens a merge dialog with diff preview, conflict detection, and strategy picker (squash/merge/rebase). Click "Show diff" for a fullscreen diff viewer with file sidebar, keyboard navigation, and font zoom
- **× Delete** — remove the worktree and its branch

Enable worktree mode by toggling the worktree switch in the new-session dialog (only appears in git repositories).

### Multi-machine setup

be-conductor supports connecting to multiple machines from a single dashboard. Each machine runs its own independent be-conductor server. The dashboard in your browser connects to all of them directly — no central hub or proxy needed.

**1. Install and start be-conductor on each machine:**

```bash
# On workstation
be-conductor run <agent> research
be-conductor run <agent> coding

# On GPU box (install be-conductor there too)
be-conductor run <agent> train
```

**2. Add machines to the dashboard:**

Open the dashboard on any device, then hamburger menu → **Settings** → **Servers** tab.

- **Tailscale device picker** — your online Tailscale devices appear in a dropdown. Select one and click Add. This is the easiest way.
- **Manual URL** — paste `http://100.x.x.x:7777` (or a MagicDNS name) for any machine on your network.
- **QR code** — run `be-conductor qr` on a machine, then use **Link Device** in the dashboard to scan it.

**How it works:**

The dashboard polls each server independently for sessions and connects via separate WebSockets. The sidebar groups sessions by machine with connection status indicators:

```
● Workstation (local)
  research
  coding
● GPU Box
  train
```

You can open terminals from different machines side by side in split view — one panel showing your workstation session, another showing your GPU box, both live.

**What happens when a machine goes offline:**

The dashboard detects the disconnect within seconds. Sessions from that machine disappear from the sidebar and any open terminal panels for it close automatically. When the machine comes back, sessions reappear.

**Persistence:**

Added machines are saved in your browser's localStorage. Refresh the page or close and reopen — your server list is preserved. Each browser/device maintains its own list independently.

### Session resume

When an agent exits and prints a resume token — like Claude Code's `--resume <session-id>` — be-conductor captures it from the terminal output automatically. The session stays in the sidebar as **resumable** with a play button. Click it (or run `be-conductor resume <name>` from the terminal) and be-conductor starts a new session with the original command plus the resume flag, picking up where you left off.

Agents that manage their own session history — like Codex (`codex resume`) and Copilot (`copilot --resume`) — are always marked as resumable when they exit. Clicking the play button launches the agent's built-in resume command.

Resume tokens are persisted to disk (`~/.be-conductor/sessions/`), so they survive server restarts and machine reboots. Power-cycle your laptop, start be-conductor again, and the resumable session is still there.

If you don't need a resumable session, dismiss it with the **×** button — a confirmation dialog prevents accidental deletion.

### Discover and observe external sessions

AI agent sessions started outside be-conductor — in IDEs, other terminals, or standalone tools — are automatically discovered and shown in the Resume tab's browse list.

be-conductor scans local session stores for **Claude Code**, **Codex**, **Copilot CLI**, **Gemini CLI**, and **Goose**. Each session shows its name/slug, project path, branch, agent badge, and recency. Sessions running in an IDE are marked with a live badge. Use the **agent filter dropdown** to narrow the list to a specific agent.

- **Resume a closed session** — select it from the list, give it a name, and click **Resume**. be-conductor launches the agent-specific resume command (e.g. `claude --resume`, `codex resume`, `copilot --resume`) in a PTY.
- **Observe a live session** — select a running session and click **Observe**. A read-only panel opens showing the conversation in real time with agent-specific formatting (user messages, assistant responses, tool calls — all color-coded). The Observe button is hidden for agents whose sessions aren't observable (Gemini, Goose).

Liveness is detected via IDE lock files (`~/.claude/ide/*.lock`, `~/.copilot/ide/*.lock`).

> **Warning:** Do not resume a session that is still active in an IDE. Session files are single-writer — resuming in be-conductor while the IDE is still using it can cause corruption. be-conductor blocks resume for sessions it detects as live, but the guard is best-effort. When in doubt, close the IDE session first.

**Creating sessions on remote machines:**

Click **+ New** in the sidebar. When multiple machines are connected, a **Machine** dropdown appears at the top of the form. Select the target machine, pick a command and directory (fetched from that machine's config), and click Run. The session starts on the remote machine and opens in a terminal panel.

**Single-server mode:**

When only one server is configured (the default), the dashboard looks and works exactly as a standalone single-machine setup. No server group headers, no machine selector — zero visual overhead.

### Remote access from another device

This requires [Tailscale](https://tailscale.com/) on both your workstation and your phone, tablet, or laptop.

**1. Start be-conductor on your workstation** (if not already running):

```bash
be-conductor run <agent> research
```

**2. Open on your other device:**

Option A — run `be-conductor qr` to show a scannable QR code:

```bash
be-conductor qr
```

Option B — use the dashboard's **Settings** → **Servers** tab to see Tailscale devices and add them.

Option C — find your Tailscale IP and type the URL:

```bash
tailscale ip -4
# 100.x.x.x
```

Then open `http://100.x.x.x:7777` on your phone.

Done. Full terminal access to all sessions from your phone — type prompts, view output, create or kill sessions. Add more machines from Settings → Servers.

### Using Tailscale MagicDNS names

Tailscale assigns each device a [MagicDNS](https://tailscale.com/kb/1081/magicdns) name like `my-workstation.tailnet-name.ts.net`. You can use these instead of IP addresses:

```
http://my-workstation.tailnet-name.ts.net:7777
```

To find your machine's name:

```bash
tailscale status
# or check the be-conductor dashboard: Settings → Servers → "This server"
```

The Servers tab in Settings shows your machine's MagicDNS name, Tailscale IP, and hostname — all fetched from the `/info` endpoint. MagicDNS names are easier to remember and don't change when IPs rotate.

### Why remote access works

Tailscale creates a private network between your devices using WireGuard. Only your devices can reach the server. No ports exposed to the internet, no passwords, no setup beyond installing Tailscale. be-conductor binds to `0.0.0.0` so it's reachable on your Tailscale network without any extra configuration.

## Is It Safe?

Yes. be-conductor runs entirely on your machines — no cloud backend, no vendor account, no external service required. Output stays local; commands run locally; nothing is logged, queued, or controlled through any third-party service.

- **No cloud dependency** — runs on your workstation, GPU box, or air-gapped network. No API keys, no SaaS backend, zero cloud costs.
- **Local only** — the server binds to your machine. Without Tailscale (or another VPN), it is not reachable from outside your local network.
- **No authentication layer needed** — when using Tailscale, only devices signed into *your* Tailscale account can reach the server. The network itself is the firewall.
- **No data leaves your machine** — session output stays in an in-memory buffer on localhost. Nothing is logged to external services.
- **Restricted dashboard commands** — the web dashboard can only launch commands from a predefined allowlist. The CLI is unrestricted, but the browser cannot start arbitrary processes.
- **Localhost-only admin by default** — the Settings admin tabs and admin API (`/admin/settings`) are only accessible from `127.0.0.1`. Set `BE_CONDUCTOR_TOKEN` (or use Settings → General → Auth Token) to allow authenticated remote access.
- **No shell injection** — session input is sent through the PTY as keystrokes, not evaluated as shell commands by be-conductor itself.
- **Sanitized session names** — names are validated against a strict allowlist (alphanumeric, hyphens, underscores, max 64 chars) on both the frontend and backend to prevent path traversal or injection via crafted names.
- **Open source (MIT)** — the entire codebase is a single Python package and a single HTML file. Read it, audit it, fork it.

If you're running be-conductor on a shared network without Tailscale, anyone on that network can reach port 7777. In that case, use a firewall rule or bind to `127.0.0.1` instead of `0.0.0.0`.

## Dashboard

The web dashboard is a single HTML file served by the be-conductor server. See [Features](#features) for the highlights. Technical details:

- **Terminal rendering** — xterm.js with colors, cursor, scrollback, minimum 80 columns (horizontal scroll on narrow panels)
- **Split view** — arbitrary panel nesting (left/right/top/bottom) with draggable dividers; layout persisted to localStorage
- **Color themes** — 6 presets per panel: Default, Dark, Mid, Bright, Bernstein, Green (retro CRT)
- **Font size controls** — per-panel `+` / `−` buttons, adaptive defaults for desktop and mobile
- **Notifications** — browser notifications and webhooks (Telegram, Discord, Slack, custom). Includes deep links to the stalled session. Smart suppression: only fires when you're not looking at the dashboard. Setup guides: [Telegram](docs/notification_telegram.md), [Slack](docs/notification_slack.md)
- **Cross-server notification sync** — push webhook config to all connected machines in one click
- **File viewer** — browse project files, view text with line numbers, render Markdown and SVG, display images, preview PDFs, download files. Clickable file paths in console output open the viewer directly
- **File upload** — drag-and-drop, clipboard paste (Ctrl+V), or attachment button (mobile). Progress dialog, path insertion, auto-cleanup on session end
- **Session discovery** — Resume tab finds external sessions from Claude Code, Codex, Copilot, Gemini, and Goose. Filter by agent, resume closed sessions, or observe live ones with agent-specific formatting
- **Settings** — tabbed dialog (General, Agents, Directories, Servers, Notifications). Admin tabs visible on localhost or with token auth. Changes propagate to all clients
- **Mobile** — extra keys toolbar (ESC, TAB, arrows, CTRL, ALT), touch scrolling, auto-fullscreen on keyboard open, collapsible sidebar
- **Auto-reconnect** — WebSocket reconnects on disconnect; update notification when a new release is available

## CLI Reference

| Command | Description |
|---|---|
| `be-conductor up` | Start the server (background daemon) |
| `be-conductor serve` | Start the server (foreground) |
| `be-conductor serve --host 0.0.0.0 --port 8888` | Custom host/port |
| `be-conductor run COMMAND [NAME]` | Start session and attach (see output in terminal) |
| `be-conductor run -w COMMAND [NAME]` | Start session in an isolated git worktree |
| `be-conductor run -d COMMAND [NAME]` | Start session in background (detached) |
| `be-conductor run --json COMMAND [NAME]` | Start session and print JSON (implies detach) |
| `be-conductor attach NAME` | Attach to a running session |
| `be-conductor resume NAME` | Resume an exited session (relaunch with resume token) |
| `be-conductor resume NAME -t TOKEN` | Resume an external agent session inside be-conductor |
| `be-conductor resume NAME -t TOKEN -c aider` | Resume with a specific agent (default: claude) |
| `be-conductor list` | List active sessions |
| `be-conductor list --json` | List sessions as JSON |
| `be-conductor status` | Show server status |
| `be-conductor status --json` | Show server status as JSON |
| `be-conductor stop NAME` | Stop a session |
| `be-conductor worktree list` | List all worktrees and their status |
| `be-conductor worktree merge NAME` | Merge a worktree back (default: squash) |
| `be-conductor worktree discard NAME` | Discard a worktree and its branch |
| `be-conductor worktree gc` | Clean up stale worktrees |
| `be-conductor shutdown` | Stop the server and all sessions |
| `be-conductor restart` | Restart the server (picks up config changes) |
| `be-conductor open` | Open the dashboard in the default browser |
| `be-conductor qr` | Show QR code (terminal + opens SVG in browser) |
| `be-conductor --help` | Show all commands |

`be-conductor run`, `be-conductor resume`, and `be-conductor open` auto-start the server as a background daemon if it isn't already running. If no name is given, the command name is used. Press `Ctrl+]` to detach from a session without stopping it.

## API

Default port `7777`. All endpoints relative to your host. OpenAPI spec at `/openapi.json`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check (`{"ok": true, "version": "..."}`) — always public |
| `GET` | `/sessions` | List all sessions |
| `GET` | `/sessions/{id}` | Get a single session |
| `POST` | `/sessions/run` | Create session (`{"name": "...", "command": "..."}`) |
| `POST` | `/sessions/{id}/input` | Send input (`{"text": "..."}` and/or `{"keys": ["CTRL+C"]}`) |
| `POST` | `/sessions/{id}/resize` | Resize PTY (`{"rows": 24, "cols": 80}`) |
| `POST` | `/sessions/{id}/upload` | Upload a file (raw body, any content type, optional `X-Filename` header) → `{"path": "...", "filename": "..."}` |
| `POST` | `/sessions/{id}/resume` | Resume an exited session with a stored resume token |
| `POST` | `/sessions/{id}/stop` | Stop a session (alias for DELETE) |
| `DELETE` | `/sessions/{id}` | Kill session (or dismiss a resumable session) |
| `WS` | `/sessions/{id}/stream` | Bidirectional WebSocket — output out, keystrokes in |
| `WS` | `/sessions/{id}/stream?typed=true` | Typed JSON WebSocket for agents |
| `GET` | `/worktrees` | List all worktrees |
| `GET` | `/worktrees/{name}/diff` | Unified diff for a worktree vs base |
| `POST` | `/worktrees/{name}/merge/preview` | Preview merge (conflicts, changed files) |
| `POST` | `/worktrees/{name}/merge` | Merge worktree (`{"strategy": "squash\|merge\|rebase"}`) |
| `DELETE` | `/worktrees/{name}` | Discard worktree and branch |
| `POST` | `/worktrees/gc` | Clean up stale/orphaned worktrees |
| `GET` | `/external/sessions` | Discover external agent sessions (optional `?project=` and `?agent=` filters) |
| `POST` | `/external/sessions/{file_id}/resume` | Resume a closed external session as a be-conductor PTY |
| `WS` | `/external/sessions/{file_id}/observe` | Read-only stream of an external session (tails JSONL, agent-aware formatting) |
| `GET` | `/worktrees/health` | Worktree health warnings |
| `GET` | `/notifications/webhook` | Get global webhook settings |
| `PUT` | `/notifications/webhook` | Update global webhook settings |
| `PUT` | `/notifications/settings` | Update per-device notification settings (`X-Device-Id` header) |
| `POST` | `/notifications/webhook/test` | Send a test notification to verify webhook config |
| `GET` | `/git/check?path=...` | Check if path is a git repo (for worktree toggle) |
| `GET` | `/info` | Server identity (hostname, port, Tailscale IP/name) |
| `GET` | `/tailscale/peers` | Online Tailscale peers for device picker |
| `GET` | `/config` | Allowed commands and default directories |
| `GET` | `/browse?path=~` | Directory listing for the directory picker |
| `GET` | `/admin/settings` | Full admin settings (localhost or token auth) |
| `PUT` | `/admin/settings` | Update settings and persist to `~/.be-conductor/config.yaml` (localhost or token auth) |
| `PUT` | `/admin/token` | Set or change the auth token (localhost only) |
| `DELETE` | `/admin/token` | Remove the auth token (localhost only) |

## Agent Integration

be-conductor exposes a stable API that AI agents and automation tools can use to start, monitor, and interact with terminal sessions programmatically.

### Discovery

Check if the server is running and get connection details:

```bash
# CLI
be-conductor status --json
# → {"ok": true, "version": "0.3.18", "base_url": "http://127.0.0.1:7777", ...}

# HTTP
curl http://127.0.0.1:7777/health
# → {"ok": true, "version": "0.3.18"}
```

The full OpenAPI spec is at `http://127.0.0.1:7777/openapi.json`.

### Start a session

```bash
curl -X POST http://127.0.0.1:7777/sessions/run \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "command": "echo hello", "source": "cli"}'
```

The response includes a `ws_url` field for streaming output.

### Stream output (typed WebSocket)

Connect to the typed WebSocket endpoint for structured JSON messages:

```
ws://127.0.0.1:7777/sessions/my-agent/stream?typed=true
```

**Server sends:**
- `{"type": "stdout", "data": "..."}` — terminal output
- `{"type": "exit", "exit_code": 0}` — session ended
- `{"type": "ping"}` — keepalive

**Client sends:**
- `{"type": "input", "data": "..."}` — text input
- `{"type": "resize", "rows": 40, "cols": 120}` — resize terminal
- Plain text fallback: non-JSON text is treated as raw input

### Send input

```bash
# Text input
curl -X POST http://127.0.0.1:7777/sessions/my-agent/input \
  -H "Content-Type: application/json" \
  -d '{"text": "yes\n"}'

# Key sequences
curl -X POST http://127.0.0.1:7777/sessions/my-agent/input \
  -H "Content-Type: application/json" \
  -d '{"keys": ["CTRL+C"]}'
```

**Supported key names:** `ENTER`, `TAB`, `ESCAPE`, `BACKSPACE`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `CTRL+A`, `CTRL+C`, `CTRL+D`, `CTRL+E`, `CTRL+K`, `CTRL+L`, `CTRL+R`, `CTRL+U`, `CTRL+W`, `CTRL+Z`, `CTRL+\`

### Resume a session

List sessions, find one by name, and reconnect:

```bash
# List all sessions
curl http://127.0.0.1:7777/sessions

# Reconnect WebSocket to an existing session
ws://127.0.0.1:7777/sessions/my-agent/stream?typed=true
```

### Authentication

Set `BE_CONDUCTOR_TOKEN` as an environment variable before starting the server:

```bash
export BE_CONDUCTOR_TOKEN=my-secret-token
be-conductor serve
```

When set, all API requests (except `/health`) require a Bearer token:

```bash
curl -H "Authorization: Bearer my-secret-token" http://127.0.0.1:7777/sessions
```

WebSocket connections accept the token as a query parameter:

```
ws://127.0.0.1:7777/sessions/my-agent/stream?typed=true&token=my-secret-token
```

When no token is configured, the API is open (same as before).

## IDE Plugins

Manage be-conductor sessions without leaving your editor. Both plugins provide session creation, live session lists, worktree management, and session persistence across IDE restarts.

| IDE | Plugin | Install |
|---|---|---|
| **JetBrains** (CLion, IDEA, PyCharm, WebStorm, GoLand, Rider, …) | [tools/jetbrains/](tools/jetbrains/be-conductor-plugin/) | Settings → Plugins → gear → Install from Disk → select `.zip` |
| **VS Code** | [tools/vscode/](tools/vscode/be-conductor-vscode/) | `code --install-extension be-conductor-launcher-0.1.0.vsix` or copy to `~/.vscode/extensions/` |

### What the plugins do

- **New Session dialog** — pick an agent from the server's command list (fetched live), enter a session name, choose a working directory, and optionally enable git worktree isolation. Runs `be-conductor run <agent> <name>` in a new terminal tab
- **Session list** — live-updating sidebar panel showing all sessions with status (running, stopping, resumable, exited). Attach, stop, resume, or dismiss sessions with toolbar buttons or right-click context menu
- **Worktree management** — view diffs in the IDE's native diff viewer, merge worktrees (squash/merge/rebase), and finalize running worktree sessions — all from the sidebar
- **Session persistence** — sessions created in the IDE are tracked per workspace/project. When you close the IDE, tracked sessions are gracefully stopped (preserving resume tokens). When you reopen, they're automatically resumed and re-attached to terminal tabs
- **Terminal integration** — sessions open in the IDE's built-in terminal. The tab stays open on errors so you can see what went wrong

### Build from source

**JetBrains** (requires Java 17+):

```bash
cd tools/jetbrains/be-conductor-plugin
./gradlew buildPlugin
# → build/distributions/be-conductor-plugin-0.2.0.zip
```

**VS Code**:

```bash
cd tools/vscode/be-conductor-vscode
npx @vscode/vsce package
# → be-conductor-launcher-0.1.0.vsix
```

## Project Structure

```
be-conductor/
├── be_conductor/
│   ├── server/app.py        # FastAPI app + static serving
│   ├── api/routes.py         # REST + WebSocket endpoints
│   ├── sessions/
│   │   ├── session.py        # Session — PTY, buffer, subscribers
│   │   └── registry.py       # In-memory session registry
│   ├── notifications/         # Notification detection, webhook dispatch
│   ├── worktrees/            # Git worktree lifecycle management
│   ├── proxy/pty_wrapper.py  # PTY spawn and I/O
│   └── utils/config.py       # Paths, ports, allowed commands
├── cli/main.py               # Click CLI
├── static/index.html          # Dashboard (single-file HTML/JS/CSS)
├── tools/
│   ├── jetbrains/            # JetBrains IDE plugin
│   └── vscode/               # VS Code extension
├── main.py                    # Entry point
├── install.sh                 # One-line installer (Linux/macOS)
├── install.ps1                # One-line installer (Windows)
├── uninstall.sh               # Uninstaller (Linux/macOS)
├── uninstall.ps1              # Uninstaller (Windows)
├── pyproject.toml
└── LICENSE                    # MIT
```

## Platform Support

| Platform | Status |
|---|---|
| Linux | Supported |
| macOS | Supported — [setup guide](docs/MACOS.md) |
| Windows | Supported (10 Build 1809+) — [setup guide](docs/WINDOWS.md) |

## Requirements

- Python 3.10+
- Linux, macOS, or Windows 10+ (PTY / ConPTY required)
- Dependencies: FastAPI, uvicorn, click, httpx, websockets, qrcode, pyte, pywinpty (Windows only)
