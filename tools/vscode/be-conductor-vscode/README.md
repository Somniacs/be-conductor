# be-conductor VS Code Extension

Status bar button and editor toolbar icon for Visual Studio Code. Pick an AI agent, name the session, and `be-conductor run <agent> <name>` opens in a new integrated terminal.

## Features

- **Editor title button** — ♭ icon in the top-right of every editor tab (like Claude Code's button)
- **Status bar item** — `$(terminal) be-conductor` in the bottom status bar
- **Agent quick pick** — all supported agents (Claude, Codex, Aider, Gemini, Copilot, OpenCode, Amp, Goose, Forge, Cursor)
- **Session name input** — validated to letters, digits, hyphens, and underscores
- **Integrated terminal** — opens in VS Code's built-in terminal with session name as tab title

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
3. The status bar button and editor title icon appear immediately

## Usage

1. Click the **♭** icon in the editor title bar, or `be-conductor` in the status bar
2. Select an AI agent from the quick pick
3. Enter a session name
4. A new terminal opens and runs the session

## Project structure

```
be-conductor-vscode/
├── package.json       # Extension manifest
├── extension.js       # Plain JS (no build step)
├── icons/
│   └── be-conductor.svg
└── README.md
```
