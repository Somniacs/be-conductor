# macOS Setup

Be-Conductor runs natively on macOS. No extra dependencies beyond Python.

## Prerequisites

- **Python 3.10+** — check with `python3 --version` (ships with Xcode CLI tools)
- **Git** — `xcode-select --install` if not already present

## Install

### One-line install (recommended)

```bash
curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash
```

The installer checks for Python 3.10+, installs [pipx](https://pipx.pypa.io/) if needed, downloads the latest release, and offers to set up autostart via launchd.

### From source

```bash
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
./install.sh
```

After install, the `be-conductor` command is available from any terminal. If the command is not found, restart your terminal or run `source ~/.zshrc`.

### Uninstall

```bash
curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/uninstall.sh | bash
```

This stops the server, removes the launchd agent, uninstalls the package, and asks whether to keep your data (`~/.be-conductor/`).

## Start sessions

```bash
be-conductor run <agent> research
be-conductor run <agent> coding
```

Open `http://127.0.0.1:7777` in Safari or any browser.

### Git worktree isolation

Run agents in isolated branches so they don't conflict:

```bash
be-conductor run -w <agent> refactor-auth
be-conductor run -w <agent> add-tests
```

Each session gets its own branch and working copy. Merge from the dashboard (with diff preview and strategy picker) or the CLI:

```bash
be-conductor worktree merge refactor-auth --strategy squash
```

Merging is non-destructive — resume the session, make more changes, and merge again. Delete the worktree when done.

### Session resume

When an agent exits with a resume token, Be-Conductor captures it. Resume from the dashboard (play button) or CLI:

```bash
be-conductor resume research
```

## Remote access from another device

1. Install [Tailscale](https://apps.apple.com/app/tailscale/id1470499037) on your Mac and your phone, tablet, or laptop
2. Sign in with the same account on all devices
3. Run `be-conductor qr` to get a scannable QR code — or open the dashboard's **Servers** dialog (hamburger menu → Servers) to discover Tailscale devices automatically

## Multi-machine setup

To monitor sessions from multiple Macs (or a mix of Mac, Linux, Windows):

1. Install and start Be-Conductor on each machine
2. Open the dashboard on any device
3. Add machines via the **Servers** dialog — Tailscale device picker, manual URL, or QR scan

All machines appear in a single sidebar, grouped by machine. Open terminals from different machines side by side in split view.

## Dashboard features

The web dashboard at `http://127.0.0.1:7777` provides:

- **Split view** — multiple terminals side by side with draggable dividers
- **Session resume** — one-click resume for agents that support it (Claude, Codex, Copilot)
- **Git worktree management** — merge dialog with diff viewer, conflict detection, squash/merge/rebase
- **Layout persistence** — panel layout saved across page reloads
- **File upload** — drag and drop, clipboard paste, or attachment button
- **Settings** — manage allowed commands and directories (localhost only)
- **Mobile-friendly** — touch scroll, extra keys toolbar, responsive layout

If you accepted autostart during install, the server already starts on boot. For manual setup or customization, see [Auto-Start on Boot](autostart.md).

## Notes

- macOS uses the same PTY (pseudo-terminal) system as Linux — full compatibility
- Tested on macOS 13+ (Ventura) with Python 3.10+
- The `be-conductor` daemon runs in the background after `be-conductor run`
- Sessions survive terminal and browser disconnects
