# be-conductor JetBrains Plugin

Session management for CLion, IntelliJ IDEA, PyCharm, WebStorm, GoLand, Rider, and all other JetBrains IDEs 2024.1+.

## Features

- **New Session dialog** — pick an agent from the server's command list (fetched live), enter a session name, choose a working directory, and optionally enable git worktree isolation
- **Session list** — live-updating sidebar panel showing all sessions with status (running, stopping, resumable, exited). Attach, stop, resume, or dismiss with toolbar buttons or right-click context menu
- **Worktree management** — view diffs in IntelliJ's native side-by-side diff viewer, merge worktrees (squash/merge/rebase), and finalize running worktree sessions
- **Session persistence** — sessions are tracked per project. On IDE close, tracked sessions are gracefully stopped (preserving resume tokens). On reopen, they're automatically resumed and re-attached to terminal tabs
- **Terminal integration** — sessions open in the IDE's built-in terminal. The tab stays open on errors so you can see what went wrong

## Requirements

- Java 17+
- Any JetBrains IDE 2024.1 or later
- The built-in Terminal plugin (enabled by default)
- `be-conductor` installed and in PATH

## Install

### From zip

1. Build the plugin (see below) or download `be-conductor-plugin-0.2.0.zip` from a [release](https://github.com/somniacs/be-conductor/releases)
2. In your IDE: **Settings → Plugins → gear icon → Install Plugin from Disk**
3. Select the zip file and restart the IDE

### Build from source

```bash
cd tools/jetbrains/be-conductor-plugin
./gradlew buildPlugin
# → build/distributions/be-conductor-plugin-0.2.0.zip
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
5. Click **OK** — a new terminal tab opens and runs the session

The **be-conductor** tool window (right sidebar) shows all sessions with live status updates. Double-click a running session to attach, or a resumable session to resume.

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
    │   │   ├── BeConductorClient.java         # HTTP client for the REST API
    │   │   └── ApiModels.java                 # Request/response models
    │   ├── toolwindow/
    │   │   ├── BeConductorToolWindowFactory.java
    │   │   ├── SessionListPanel.java          # Session list + actions
    │   │   ├── WorktreeListPanel.java         # Worktree list + actions
    │   │   └── DiffViewerUtil.java            # Native diff viewer integration
    │   └── dialogs/
    │       └── MergeDialog.java               # Merge strategy picker
    └── resources/
        ├── META-INF/plugin.xml
        └── icons/be-conductor.svg
```
