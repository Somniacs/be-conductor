# Changelog

All notable changes to Be-Conductor are documented here.

## v0.3.13

### Session persistence across IDE restarts

- **Auto-resume on IDE open** — sessions that were running when you closed the IDE are gracefully stopped (preserving resume tokens), then automatically resumed and re-attached when the IDE reopens. Works in both VS Code and JetBrains
- **Graceful shutdown on IDE close** — tracked sessions receive a graceful stop signal when the project closes, giving agents time to print their resume tokens
- **Session tracking** — sessions created or attached in the IDE are tracked per-workspace/project. Manually killed, forgotten, or dismissed sessions are untracked so they don't auto-resume

### Terminal output fix

- **Fixed garbled TUI output** — rapid terminal updates (e.g. Claude Code's agent progress tree, spinners) could garble the display when proxied through be-conductor. The subscriber queue was silently dropping data when full, breaking ANSI escape sequences mid-stream. The queue now coalesces pending items instead of dropping, and the WebSocket writer batches rapid bursts into atomic sends
- **VSCode plugin: theme-aware icon** — the activity bar icon now uses `currentColor` so it adapts to light and dark themes instead of being hardcoded
- **VSCode plugin: no auto-resume on click** — clicking a resumable session no longer auto-resumes it; use the play button instead
- **VSCode plugin: forgetSession** — added "Forget Session" context menu action for running sessions

## v0.3.12

### IDE plugins (v0.2.0) — session & worktree management

Both JetBrains and VS Code plugins grew from simple "launch a session" buttons into full session management panels:

- **Sidebar panels** — live-updating session list and worktree list right in the IDE. See which sessions are running, stopped, or resumable at a glance. Attach, stop, resume, or dismiss sessions with one click
- **Worktree management** — view, finalize, merge, diff, and delete worktrees without leaving the editor
- **Native diff viewer** — review worktree changes in IntelliJ's or VS Code's built-in side-by-side diff editor
- **Smarter session creation** — the dialog now pulls available commands from the server (no more hardcoded agent list), lets you pick a working directory, and optionally isolate the session in a git worktree with branch name preview
- **Auto-refresh** — session and worktree lists poll the server automatically (VS Code only polls while the sidebar is visible)

### Bug fixes

- **Fixed mobile terminal scroll jumping on every keystroke** — on narrow screens, typing would snap the viewport back to the left after each keypress. The browser was fighting the horizontal scroll position by trying to reveal a hidden input element. Now fixed — typing scrolls smoothly with the cursor

### Mobile terminal

- **Horizontal auto-scroll to cursor** — on narrow screens where the terminal is wider than the viewport, the view now follows the cursor as you type. Scrolls left and right to keep the cursor visible, but stays put during passive output so log streams don't cause jarring jumps

### Backend

- **Rich diff API** — new endpoint for IDE plugins to fetch per-file before/after content for native diff viewers
- **Cleaner session shutdown** — stop and kill signals are now more reliable, with proper error logging for background tasks and non-blocking cleanup

## v0.3.11

### IDE plugins

- **JetBrains plugin** — toolbar button for CLion, IntelliJ IDEA, PyCharm, WebStorm, GoLand, and all other JetBrains IDEs. Opens a dialog to pick an AI agent and name the session, then runs `be-conductor run <agent> <name>` in a new terminal tab. Build: `cd tools/jetbrains/be-conductor-plugin && ./gradlew buildPlugin`. Targets JetBrains 2024.1+, Java 17+
- **VS Code extension** — quick pick for agent selection with "Open Dashboard" at the top, input box for session name, opens an integrated terminal. No build step needed (plain JS). Install: symlink or copy to `~/.vscode/extensions/`
- **Session name validation** — both plugins now validate session names against the backend regex (must start alphanumeric, max 64 chars, allows spaces/dots/tildes)
- **Release artifacts** — the release workflow now builds and attaches the VS Code extension (`.vsix`) and JetBrains plugin (`.zip`) alongside the existing archives

### CLI improvements

- **`qr` auto-start** — `be-conductor qr` now starts the server daemon automatically if it isn't running, matching the behavior of `run` and `open`
- **Localhost URL in QR output** — `be-conductor qr` now prints the local `http://127.0.0.1:7777` URL alongside the Tailscale URL, both in the terminal and on the QR HTML page, so you can click to open directly
- **Clickable links on QR page** — URLs on the browser QR page are now clickable `<a>` links instead of plain text
- **QR page charset fix** — added `<meta charset="utf-8">` so the ♭ symbol renders correctly in the browser tab title

### UI

- **Sidebar title** — drawer header now shows `♭ conductor` instead of `♭ be-conductor` (the flat sign already represents "be")
- **About dialog title** — fixed to match the sidebar title ("♭ conductor")

## v0.3.10

### Project rename: conductor → be-conductor

- **New name** — the project is now **be-conductor** everywhere: CLI command, package name, data directory (`~/.be-conductor/`), environment variable (`BE_CONDUCTOR_TOKEN`), GitHub repo ([somniacs/be-conductor](https://github.com/somniacs/be-conductor))
- **Automatic migration** — on first run, `~/.conductor/` is moved to `~/.be-conductor/` automatically. The old `CONDUCTOR_TOKEN` env var still works (with a deprecation warning)
- **Installer migration** — `install.sh` / `install.ps1` detect old `conductor` installations: stop the old server, remove old autostart services, uninstall the old package, and migrate the data directory
- **New repo** — code now lives at [github.com/somniacs/be-conductor](https://github.com/somniacs/be-conductor). The old `somniacs/conductor` repo points here

## v0.3.9

### One-line installer and uninstaller

- **One-line install** — `curl -fsSL .../install.sh | bash` (Linux/macOS) or `irm .../install.ps1 | iex` (Windows) downloads the latest release, installs via pipx, and offers to set up autostart. Also works as a local installer when run from a cloned repo or extracted tarball (detects `pyproject.toml` and uses `pipx install -e`)
- **Autostart prompt** — the installer asks whether to configure autostart on boot: systemd user service on Linux, launchd agent on macOS, Task Scheduler on Windows. Previously required manual setup from the [autostart docs](docs/autostart.md)
- **Uninstaller** — `curl -fsSL .../uninstall.sh | bash` (Linux/macOS) or `irm .../uninstall.ps1 | iex` (Windows) stops the server, removes autostart configs, uninstalls the package via pipx, and asks whether to keep or remove user data. Also available as `./uninstall.sh` or `uninstall.ps1` from the repo
- **Release artifacts** — `install.sh`, `uninstall.sh`, `install.ps1`, and `uninstall.ps1` are now uploaded alongside the tarball and zip on every release
- **Rename-ready** — all scripts use variables at the top (`PROJECT`, `REPO`, etc.) with an `OLD_PROJECT` field for seamless migration — stops old server, removes old autostart, moves data directory, uninstalls old package

### Multi-agent session discovery and observation

- **Multi-agent discovery** — the Resume tab now discovers sessions from Claude Code, Codex, Copilot CLI, Gemini CLI, and Goose. Each agent's local session storage is scanned automatically (JSONL files, SQLite databases, YAML metadata). Sessions are shown in a unified list sorted by recency
- **Agent filter** — a new dropdown in the browse list lets you filter by agent (Claude, Codex, Copilot, etc.). Agent badges with color-coded labels appear on each session item
- **Agent-specific observation** — the read-only observation panel now formats JSONL records differently per agent: Claude Code (user/assistant/tool_use), Codex (event_msg/response_item), and Copilot (user.message/assistant.message with tool requests)
- **Agent-specific resume** — each agent uses its own resume command (e.g. `claude --resume`, `codex resume`, `copilot --resume`). The resume endpoint reads the command from the scanner instead of hardcoding
- **Discover IDE sessions** — scans `~/.claude/projects/` for Claude Code, `~/.codex/state_5.sqlite` for Codex, `~/.copilot/session-state/` for Copilot, with defensive stubs for Gemini (`~/.gemini/tmp/`) and Goose (`~/.local/share/goose/`). Sessions running in VS Code or JetBrains IDEs are detected via lock files and shown with a live badge
- **Observe live sessions** — select a running session to open a read-only observation panel. The JSONL file is tailed in real-time with ANSI-colored formatting. Sessions without a JSONL file (Gemini, Goose) hide the Observe button
- **Session scanner** — `conductor.external` package handles discovery across all agents (10s cache, excludes subagent files, filters out sessions already in Be-Conductor) and observation (history limited to last 200 records, auto-cleanup on disconnect)
- **Security** — file IDs use an `agent::id` namespace format. Bare UUIDs are accepted for backward compatibility (mapped to Claude). IDs are validated against a strict allowlist of agent prefixes

### Notification system

- **Server-side notification detection** — uses a pyte virtual terminal to maintain a clean screen representation (same as what xterm.js renders in the browser), then pattern-matches against it to detect when an agent is waiting for user input (confirmation prompts, permission requests, questions). Scanning runs after 5 seconds of silence, with a 60-second cooldown to prevent spam
- **Browser notifications** — opt-in system notifications when an agent needs attention (only fires when the tab is not visible). Uses Service Worker for mobile browser support. Configurable per device via the Notifications settings tab
- **Audio alerts** — optional notification chime that plays when an agent is waiting. Preview button in settings to hear the sound before enabling
- **Webhook integration** — send notifications to Telegram, Discord, Slack, or any custom JSON endpoint. Platform dropdown with per-platform fields (e.g. Telegram shows Bot Token + Chat ID instead of a raw URL). Includes a test button to verify configuration. Setup guides: [Telegram](docs/notification_telegram.md), [Slack](docs/notification_slack.md)
- **Dashboard deep links** — webhook messages include a clickable "Open session" link that opens the dashboard directly to the specific session (`#session=<name>`). Works with Telegram, Slack, Discord, and generic JSON webhooks. Tap the link on your phone and you're looking at the terminal
- **Smart webhook suppression** — webhooks only fire when you're not already looking. If the dashboard tab is focused, an ack is sent back to the server and the webhook is skipped (like WhatsApp read receipts). If the tab is hidden, minimised, or the browser window is behind other apps, the webhook fires after a 2-second grace period
- **Claude Code selection menus** — notification patterns now detect Claude Code's `❯ 1. Yes / 2. No` selection prompts and multi-option menus (`Enter to select · ↑/↓ to navigate`). The scan window was widened from 3 to 5 bottom lines to cover the full prompt area
- **Context-aware snippets** — webhook notification snippets now show the actual prompt content instead of UI hint lines. When a match hits a hint line (like `Enter to select`), the snippet walks upward to find the meaningful context
- **Global webhook settings** — webhook configuration is stored server-side and shared across all devices. Change it on your phone, see it on desktop. Browser/sound preferences remain per-device (localStorage)
- **Masked secrets** — bot tokens, chat IDs, and webhook URLs are displayed as password fields with an eye toggle to reveal/hide
- **Settings visible to all devices** — the Settings dialog is now accessible from any device (not just localhost). Remote devices see only the Notifications tab; admin tabs (Agents, Directories, General) remain localhost-only
- **Custom notification patterns** — per-agent `notification_patterns` can be configured to match agent-specific prompts beyond the built-in defaults

### UI fixes

- **Empty state action** — restored the "+ New Session" button on the empty state screen (lost during the v0.3.8 rework)
- **Scrollable new-session dialog** — the new-session form now scrolls on small viewports so the Run button is always reachable

### New agents

- **Gemini CLI** — Google's terminal AI agent (`gemini`), with resume support via `gemini --resume`
- **OpenCode** — open-source AI coding agent (`opencode`), with resume support via `opencode --continue`
- **Amp** — Sourcegraph's AI coding agent (`amp`)
- **Forge** — open-source pair-programming agent (`forge`)
- **Goose resume** — Goose (Block) sessions are now resumable via `goose session --resume`

The default command list now includes 12 agents. All agents support git worktree isolation out of the box. Users with a saved `~/.be-conductor/config.yaml` keep their own list — reset via Settings → "Reset to defaults" to pick up the new agents.

### Settings tabs

- **Tabbed settings dialog** — the Settings panel is now organized into three tabs: Agents, Directories, and General. Makes the growing command list easier to manage
- **Resume command field** — the command editor now includes a "Resume command" field for agents that manage their own session history (e.g. `gemini --resume`, `opencode --continue`)

## v0.3.8

### Resume support

- **Resume from dashboard** — New/Resume toggle in the new-session dialog; in resume mode, paste an external resume token (e.g. from Claude's `--resume` output) to pick up the conversation inside Be-Conductor. Command-based agents (Codex, Copilot) show their resume command automatically — no token needed
- **Multi-agent resume** — `be-conductor resume --token` and dashboard resume now work with any agent via the `--command` flag (defaults to claude); reads `resume_flag` from server config per agent
- **Command-first dialog** — new-session dialog now shows the command picker before the session name, matching the CLI argument order

### File uploads

- **Upload progress bar** — file uploads now show a real-time progress bar with loaded/total MB and percentage (uses XMLHttpRequest for progress events)
- **Configurable upload warning** — no hard upload size limit; files over the configured threshold (default 20 MB) prompt for confirmation instead of blocking. Threshold is adjustable in Settings ("Upload warning")

### Worktree UX overhaul

- **Worktrees are normal sessions** — worktree sessions now behave exactly like regular sessions: same play/stop buttons, same terminal handling, no special read-only mode
- **Non-destructive merge** — merge a worktree into its base branch, then resume and keep working; merge again as many times as needed. The worktree stays alive until you explicitly delete it
- **Merge button visibility** — the ↻ merge button only appears when there are actual commits to merge; disappears after a successful merge and reappears when new changes are committed
- **Merge busy dialog** — blocking spinner during merge operations to prevent interaction while the merge runs
- **Fullscreen diff viewer** — "Show diff" in the merge dialog opens a dedicated fullscreen overlay with:
  - File sidebar on the left with per-file addition/deletion counts
  - ▲/▼ navigation buttons and keyboard shortcuts (↑/↓ or j/k) to jump between files
  - File position indicator (e.g. "1 / 5")
  - Font zoom controls (A−/A+) with keyboard shortcuts (+/−), range 8px–24px
  - Color-coded diff lines: green additions, red deletions, blue hunks, amber file headers
  - Responsive: sidebar hidden on mobile, header wraps with close button always accessible
  - Escape to close
- **No finalize step** — removed the finalize concept; merge and discard are always available directly
- **Worktree branch icon** — larger git-branch icon on the left side of worktree session items; branch name only shown in subtitle when it differs from the session name
- **Discard via × button** — the dismiss button on exited worktree sessions triggers discard (with confirmation), matching the normal session pattern
- **Live commits_ahead** — worktree commit count is refreshed on every session list fetch, so the sidebar always reflects the current git state

### Dashboard UI

- **Busy dialogs** — stop, resume, and create operations show a blocking spinner dialog to prevent interaction during async transitions; auto-dismisses after 10 seconds if something goes wrong
- **Layout persistence** — open panels, split layout, and focus are saved to localStorage and restored on page reload; only panels with running sessions are restored
- **Custom dialogs** — all notifications and confirmations use themed in-app dialogs; no browser-native alert/confirm popups anywhere
- **New session opens full-screen** — creating or resuming a session closes all existing panels and opens the new one as the only terminal
- **Play button styling** — dimmed green by default, bright on hover; removed redundant spinner badges from sidebar items
- **Server connection state** — server dots show an unfilled/unknown state on page load; only colored green or red once the connection is confirmed

### Mobile fixes

- **Layout restore on mobile** — saved layout is correctly restored after page reload; sidebar no longer opens on top of restored panels
- **Drawer no longer pops open** — automated panel cleanup (orphan/stale server removal) no longer triggers the sidebar drawer; drawer only opens from deliberate user actions
- **Cursor scroll on focus** — tapping into a terminal on mobile now scrolls the cursor into view on the first tap (previously required a second tap due to a resize timing issue during the split-to-single-view transition)

## v0.3.7

### Git worktree isolation

- **Worktree sessions** — run any agent (Claude Code, Aider, Codex, Goose, Copilot, etc.) in an isolated git worktree so each session gets its own branch and working copy — no conflicts between parallel agents or your own work. Auto-commits on exit, merge back with squash/merge/rebase strategies
- **Worktree CLI** — `be-conductor run -w` to start a worktree session; `be-conductor worktree list|merge|discard|gc` to manage them
- **Worktree dashboard** — worktree toggle in new-session dialog, color-coded badge pill (green = active, blue = finalized, red = orphaned, orange = stale) with branch name and commit count. Finalized sessions persist in the sidebar until merged or discarded
- **Worktree diff view** — "diff" button on active and finalized worktrees opens a syntax-highlighted unified diff dialog (additions in green, deletions in red, file headers in amber, hunks in blue)
- **Worktree finalize button** — "finalize" button on active worktree sessions gracefully stops the agent, auto-commits changes, and keeps the session in the sidebar for merge/discard

### File uploads

- **Desktop drag-and-drop upload** — drag files directly onto a terminal panel to upload; also supports clipboard paste (Ctrl+V) and the panel header attachment button
- **Desktop upload button** — paperclip icon in the panel header for file uploads on desktop (touch devices use the existing extra-keys button)

### Dashboard UI

- **Machine icons** — sidebar server group headers now show a monitor icon for clearer visual distinction from session items
- **Empty state action** — the "Select a session or create a new one" screen now includes a "+ New Session" button to create a session directly
- **Panel overflow menu** — header actions (theme, upload, font size, maximize) collapsed into a "⋯" menu; only the close button remains in the header bar
- **Move panel** — rearrange panels in the layout via directional arrows (← → ↑ ↓) in the overflow menu
- **Cleaner resumable sessions** — removed redundant red "resumable" badge from sidebar; the green play button is sufficient

### CLI

- **CLI resume** — `be-conductor resume <name>` resumes an exited session from the terminal, attaching automatically (use `-d` to resume in background)
- **Restart/shutdown safety** — `be-conductor restart` and `be-conductor shutdown` now warn about active sessions before killing them; pass `-f` to skip
- **Resume auto-start** — `be-conductor resume` now auto-starts the server daemon if it isn't running, matching `be-conductor run` and `be-conductor open`
- **External resume** — `be-conductor resume <name> --token <UUID>` brings an external Claude session into Be-Conductor; start Claude in any terminal, exit, copy the UUID from its `--resume` output, then resume it inside Be-Conductor

### Fixes

- Fixed browser-created sessions ("+New" in UI) showing the cursor ~2 lines below its actual position — replaced `fitAddon.fit()` with unified manual cell measurement matching the working CLI code path; also affected desktop when resizing the sidebar
- Fixed extra-keys bar staying visible after cancelling the file picker on mobile/tablet
- Fixed upload dialog overflowing on small phone screens
- Queue overflow in subscriber broadcast now logs a warning instead of silently dropping output

## v0.3.6

### Fixes

- Fixed cursor appearing one line too low on mobile — uses actual rendered cell height to prevent sub-pixel rounding from allocating an extra row

## v0.3.5

### New features

- **Tablet support** — touch scrolling, extra-keys bar, and custom scrollbars now work on tablets (previously only activated below 700px width); uses `pointer: coarse` media query to detect touch devices without affecting touchscreen laptops
- **Keyboard-aware extra keys** — extra-keys bar appears when the virtual keyboard opens and positions itself above it, including on tablets in desktop browser mode (uses the Visual Viewport API with focusin fallback)
- **Maximize panel** — double-click a session title bar to maximize that panel; double-click again or click any session in the sidebar to restore the split layout
- **Open panel indicators** — sessions placed in the view show a highlighted left border in the sidebar, so you can tell which sessions are open vs unplaced

### Fixes

- Extra-keys drawer expand/collapse now correctly resizes the terminal in all modes
- Body height properly accounts for extra-keys bar when the keyboard is open in desktop browser mode

## v0.3.4

### New features

- **Smooth native scrolling** — one-finger scrolling on mobile is now hardware-accelerated with native momentum, replacing the custom JavaScript scroll handler for dramatically lower latency
- **Focused panel on mobile** — when the keyboard opens with multiple panels, only the active panel is shown at full size; the split layout restores when the keyboard closes
- **Compact extra keys** — reduced vertical size of the mobile extra-keys bar so more terminal content is visible

### Fixes

- Mobile terminal no longer shifts upward after scrolling
- Faster reconnection on mobile (reduced from 2s to 500ms)

## v0.3.3

### New features

- **Combined touch scroll** — vertical and horizontal scrolling work simultaneously on mobile with momentum on both axes; no direction locking
- **Horizontal scrollbar** — scroll indicator at the bottom of the terminal shows when content is wider than the panel
- **Sidebar version** — current version shown next to the title in the sidebar
- **Tap to scroll** — tapping the terminal on mobile scrolls to the cursor position
- **Shift modifier** — Shift button (⇧) on the mobile extra-keys bar enables Shift+Tab, Shift+Arrow, and other modified key sequences (useful for edit mode in Claude/Codex)
- **Extra-keys layout** — added pipe (|) key; Tab and Shift use Unicode symbols (⇥/⇧); ↑ and ↓ arrows are vertically aligned across rows

### Fixes

- `be-conductor run` now sends the caller's working directory to the server, so sessions start in the correct directory instead of the server's cwd
- Mobile sidebar drawer now closes when creating or resuming a session (previously only closed when opening an existing one)
- Extra-keys drawer toggle now works reliably on mobile after collapsing
- Custom scrollbar drag now works on mobile (was mouse-only; added touch event support)
- Extra-keys drawer no longer overlaps terminal content; terminal resizes to fit above keyboard and drawer
- Vertical touch scroll now works reliably when the virtual keyboard is open
- Horizontal scrollbar now updates immediately after terminal resize instead of waiting up to 500ms
- Terminal now resizes correctly when the mobile keyboard opens — switched to `interactive-widget=resizes-content` so the layout viewport shrinks with the keyboard; title bar stays visible, scrollbars stay within bounds
- Drag-and-drop overlay no longer triggers on internal element drags (only activates for external file drops)
- One-finger touch scroll is now immediate (removed rAF batching that added a frame of input latency); two-finger gestures no longer cause scroll position to jump back on release

## v0.3.2

- **Update notification** — the dashboard checks GitHub for new releases on load and shows a subtle banner at the bottom of the sidebar when an update is available; click to open the release page
- **Reconnect spinner** — the "Server disconnected" status bar now shows a spinning indicator instead of static text
- **Codex resume support** — Codex sessions are always resumable; clicking the play button runs `codex resume`. Added `codex --full-auto` variant with `codex resume --last`
- **Copilot resume support** — GitHub Copilot CLI sessions are always resumable via `copilot --resume` (picker) or `copilot --continue` (most recent). Command changed from `gh copilot` to `copilot` (standalone binary). Added `copilot --allow-all-tools` variant
- **Command-based resume** — new `resume_command` field for agents that manage their own session history (no token extraction needed); used by Codex and Copilot
- **Graceful stop improvements** — SIGINT-first kill prevents Node runtime crashes (Codex); reduced stop sequence delay from 2s to 1s; Copilot uses direct SIGINT instead of PTY text commands for instant shutdown
- **Orphan panel cleanup** — terminal panels are automatically closed when their session disappears from the server
- **Settings reset** — "Reset to defaults" button in the Settings dialog restores built-in command list, directories, and all other settings
- **Unified versioning** — version defined in one place (`pyproject.toml`); backend reads via `importlib.metadata`, frontend fetches from `/info`

## v0.3.1

- **Admin settings panel** — localhost-only Settings dialog in the web dashboard for managing allowed commands, default directories, buffer size, upload limits, and stop timeout. Changes persist to `~/.be-conductor/config.yaml` and propagate to all connected clients automatically
- **Admin API** — `GET /admin/settings` and `PUT /admin/settings` endpoints (localhost-only, returns 403 for remote clients)
- **Config file** — settings now stored in `~/.be-conductor/config.yaml`, loaded at startup, merged over built-in defaults
- **Live config updates** — config version tracking via `X-Config-Version` header; all dashboard clients auto-refresh when settings change
- **Terminal resize fix** — split-view panels now resize without cursor drift or spurious scrollbars; rows always match the visible area while columns match the PTY for correct line wrapping
- **Cursor position fix** — eliminated resize oscillation by reading cell dimensions directly from the xterm renderer instead of calling `fit()`, so a single resize per layout change keeps the cursor in place
- **Mobile touch scroll** — direction-locked one-finger scroll (vertical or horizontal) with momentum; `touch-action: none` prevents the browser from hijacking diagonal gestures
- **Mobile horizontal scroll** — wide terminal output scrolls horizontally via the same touch handler when content overflows the panel width
- **Extra-keys modifiers** — Ctrl and Alt buttons on the mobile extra-keys bar now work with virtual keyboard input (e.g. Ctrl+O, Ctrl+C, Alt+F); modifiers auto-clear after each keystroke
- **Extra-keys overlay** — collapsed mobile keys handle overlays the terminal at reduced opacity instead of reserving vertical space
- **Extra-keys positioning** — bar now tracks the visual viewport on mobile so it stays above the keyboard in split-view lower panels instead of jumping to the top of the screen
- **UI contrast** — bumped muted text colors across the dashboard for better readability in sunlight
- **Auth token hint** — Settings dialog shows setup instructions when `BE_CONDUCTOR_TOKEN` is not set
- **Stable Tailscale URLs** — all server connections (Tailscale picker, manual input, QR scanner, QR code dialog, CLI `be-conductor qr`) now use MagicDNS names instead of bare IPs, so saved servers survive IP changes
- **Tailscale peer names** — devices that report "localhost" as hostname (e.g. Android) now show the MagicDNS device name in the picker instead
- **Robust server shutdown** — `be-conductor shutdown` now finds the server process via `pgrep` when the PID file is missing
- **CLI `--version` flag** — `be-conductor --version` prints the current version
- **Auto-start docs** — setup guide for systemd (Linux), launchd (macOS), and Task Scheduler (Windows)
- **CHANGELOG.md** — added changelog with history from v0.1.0
- **README** — table of contents, refined intro and cloud-independence positioning, autostart reference

## v0.3.0

First public release.

- **Web terminal rendering** — custom scrollbar, correct PTY dimensions on buffer replay, full-height terminal panels
- **Graceful stop & resume** — stop sequence support, resume token capture from terminal output, persistent resume across reboots
- **Session creation from dashboard** — pick agent, directory, and target machine; start sessions without a terminal
- **Multi-machine dashboard** — connect to multiple Be-Conductor servers, sessions grouped by machine with status indicators
- **Tailscale device picker** — discover and add machines from your Tailscale network
- **File upload** — paste, drag-and-drop, or attachment button; upload dialog with progress; auto-cleanup on session end
- **Mobile extra keys** — on-screen toolbar (ESC, TAB, arrows, CTRL, ALT, etc.) above the virtual keyboard, with collapsible drawer
- **Mobile touch scroll** — one-finger scroll with momentum in terminal panels
- **Split view** — binary tree panel layout with directional placement and draggable dividers
- **Performance** — async Tailscale lookups, incremental session list rendering, fetch timeouts for offline servers
- **CLI** — `--version` flag, `run` passes initial terminal size to PTY, `attach` syncs terminal dimensions
- **Security** — session name sanitization, command allowlist enforcement, bearer token auth
- **Platform support** — Linux, macOS, Windows 10+ (ConPTY)

## v0.2.1

- Upload dialog for file sharing with sessions
- Mobile extra keys toolbar with persistent expand/collapse state
- One-finger touch scroll with momentum for mobile terminals
- WebSocket auth fix for bearer token middleware

## v0.1.3

- Session resume support — captures resume tokens from terminal output, persists across restarts
- Hostname display for local server in multi-server sidebar

## v0.1.2

- Multi-server dashboard — connect to multiple machines from a single browser
- Tailscale device picker in Servers dialog
- License headers and security hardening
- README rewrite with generic agent examples

## v0.1.1

- Binary tree panel layout with directional placement menu
- Mobile placement menu support
- GitHub org migration (xohm → somniacs)
- Session name sanitization

## v0.1.0

Initial internal release.

- Terminal session management via PTY
- Web dashboard with xterm.js
- CLI for run, attach, list, stop, shutdown
- WebSocket streaming (raw and typed JSON)
- Theme presets, font size controls, idle notifications
- QR code for device linking
- Tailscale remote access
- Install scripts for Linux, macOS, Windows
