# be-conductor JetBrains Plugin

Session management for CLion, IntelliJ IDEA, PyCharm, WebStorm, GoLand, Rider, and all other JetBrains IDEs 2024.1+. Connect to multiple machines and manage sessions across your entire fleet.

## Features

- **New Session dialog** — pick an agent from the server's command list (fetched live), enter a session name, choose a working directory, and optionally enable git worktree isolation. GUI sessions open as dockable JCEF panels, terminal sessions open in the IDE's terminal
- **Session list** — live-updating sidebar panel showing all sessions with status (running, stopping, resumable, exited). Attach, stop, resume, or dismiss with toolbar buttons or right-click context menu
- **Worktree management** — view diffs in IntelliJ's native side-by-side diff viewer, merge worktrees (squash/merge/rebase), and finalize running worktree sessions
- **Session persistence** — sessions are tracked per project. On IDE close, tracked sessions are gracefully stopped (preserving resume tokens). On reopen, they're automatically resumed and re-attached
- **Terminal integration** — PTY sessions open in the IDE's built-in terminal. The tab stays open on errors so you can see what went wrong

## Multi-Server Support

Connect to multiple be-conductor servers and manage sessions across all your machines from one IDE window.

- **Servers tab** — third tab in the tool window alongside Sessions and Worktrees. Shows all configured servers with online/offline status and version
- **Add servers** — add remote servers by URL, or scan your Tailscale network to discover machines automatically
- **Tailscale discovery** — the Servers tab includes a Tailscale panel that lists available peers. Select a peer and click "Add Selected" to connect
- **Sessions grouped by machine** — when multiple servers are enabled, the session list groups sessions under server headers with session counts. Single-server mode keeps the flat list unchanged
- **Server picker in New Session** — when creating a session with multiple servers, a server dropdown is added to the dialog
- **Right-click actions** — Test Connection, Rename, Enable/Disable, Remove

## Requirements

- Java 17+
- Any JetBrains IDE 2024.1 or later
- The built-in Terminal plugin (enabled by default)
- `be-conductor` installed and in PATH

## Install

### From zip

1. Build the plugin (see below) or download from a [release](https://github.com/somniacs/be-conductor/releases)
2. In your IDE: **Settings → Plugins → gear icon → Install Plugin from Disk**
3. Select the zip file and restart the IDE

### Build from source

```bash
cd tools/jetbrains/be-conductor-plugin
./gradlew buildPlugin
# → build/distributions/be-conductor-plugin.zip
```

### Development

```bash
# Launch a sandboxed IDE instance with the plugin loaded
./gradlew runIde

# Validate plugin.xml
./gradlew verifyPlugin
```

## Usage

1. Click the **♭** button in the main toolbar (or **Tools → New Session**)
2. Select an AI agent from the dropdown
3. Enter a session name and working directory
4. Optionally toggle worktree isolation
5. Choose GUI or Terminal mode
6. Click **OK** — the session opens and appears in the sidebar

The **be-conductor** tool window (right sidebar) shows three tabs: **Sessions**, **Worktrees**, and **Servers**, all with live status updates. Double-click a running session to attach, or a resumable session to resume.

## Project structure

```
be-conductor-plugin/
├── build.gradle
├── settings.gradle
├── gradle.properties
├── gradlew / gradlew.bat
├── gradle/wrapper/
└── src/main/
    ├── java/com/somniacs/beconductor/
    │   ├── RunSessionAction.java              # Toolbar action
    │   ├── NewSessionDialog.java              # Agent picker + session config dialog
    │   ├── OpenDashboardAction.java           # Open web dashboard
    │   ├── SessionPersistenceListener.java    # Graceful stop on project close
    │   ├── AppShutdownListener.java           # Graceful stop on IDE shutdown
    │   ├── api/
    │   │   ├── BeConductorClient.java         # HTTP client (multi-server)
    │   │   ├── ApiModels.java                 # Request/response models
    │   │   └── ServerRegistry.java            # Multi-server registry (persistent)
    │   ├── toolwindow/
    │   │   ├── BeConductorToolWindowFactory.java
    │   │   ├── SessionListPanel.java          # Session list + actions (grouped by server)
    │   │   ├── WorktreeListPanel.java         # Worktree list + actions
    │   │   ├── ServerListPanel.java           # Server management panel
    │   │   └── DiffViewerUtil.java            # Native diff viewer integration
    │   ├── agent/
    │   │   ├── AgentFileEditor.java           # JCEF agent panel (editor tab)
    │   │   ├── AgentFileEditorProvider.java   # FileEditor registration
    │   │   ├── AgentSessionPanel.java         # JCEF agent panel (tool window fallback)
    │   │   └── AgentWebSocketClient.java      # WebSocket client for agent events
    │   └── dialogs/
    │       └── MergeDialog.java               # Merge strategy picker
    └── resources/
        ├── META-INF/plugin.xml
        └── icons/be-conductor.svg
```
