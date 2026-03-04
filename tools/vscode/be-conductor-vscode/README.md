# be-conductor VS Code Extension

Session management for Visual Studio Code. Create, attach, stop, resume, and manage worktree sessions — all from the sidebar.

## Features

- **New Session** — pick an agent from the server's command list (fetched live), enter a session name, choose a working directory, and optionally enable git worktree isolation. Runs in a new integrated terminal
- **Session tree** — live-updating sidebar showing all sessions with status icons. Attach, stop, resume, dismiss, or forget sessions via inline buttons and context menu
- **Worktree tree** — sidebar panel listing worktrees with branch name and commit count. View diffs in VS Code's native diff editor, merge (squash/merge/rebase), finalize, or delete worktrees
- **Session persistence** — sessions are tracked per workspace. On IDE close, tracked sessions are gracefully stopped (preserving resume tokens). On reopen, they're automatically resumed and re-attached to terminal tabs
- **Status bar** — shows server connection status; click to create a new session
- **Theme-aware icon** — activity bar icon adapts to light and dark themes

## Requirements

- VS Code 1.85 or later
- `be-conductor` installed and in PATH

## Install

### From .vsix

```bash
cd tools/vscode/be-conductor-vscode
npx @vscode/vsce package
code --install-extension be-conductor-launcher-0.1.0.vsix
```

### Manual copy

Copy the extension folder to your VS Code extensions directory:

```bash
cp -r tools/vscode/be-conductor-vscode ~/.vscode/extensions/be-conductor-launcher-0.1.0
```

Restart VS Code.

### Development

1. Open `tools/vscode/be-conductor-vscode/` in VS Code
2. Press **F5** to launch an Extension Development Host
3. The status bar button and sidebar panels appear immediately

## Usage

1. Click the **♭** icon in the editor title bar, or `be-conductor` in the status bar
2. Select an AI agent from the quick pick
3. Enter a session name
4. A new terminal opens and runs the session

The **be-conductor** sidebar (activity bar icon) shows two panels: **Sessions** and **Worktrees**, both with live-updating status and inline action buttons.

## Project structure

```
be-conductor-vscode/
├── package.json             # Extension manifest
├── extension.js             # Main lifecycle (activate/deactivate)
├── src/
│   ├── api.js               # HTTP client for the REST API
│   ├── config.js            # Server URL and settings
│   ├── createSession.js     # New session flow + session tracking
│   ├── sessionTree.js       # Session tree data provider + commands
│   └── worktreeTree.js      # Worktree tree data provider + commands
├── icons/
│   └── be-conductor.svg
└── README.md
```
