# Quick Start

Control your AI agents from your phone in 5 minutes.

## 1. Set up Tailscale

Tailscale is a free app that creates a private network between your devices. Install it once, and your phone can reach your computer from anywhere.

### Create an account

Go to [tailscale.com](https://tailscale.com/) and create a free account (you can sign in with Google, Microsoft, GitHub, etc.).

### Install on your computer

**Linux:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the link it prints to sign in from your browser.

**macOS:**

Download from the [Mac App Store](https://apps.apple.com/app/tailscale/id1475387142), or with Homebrew:

```bash
brew install --cask tailscale
```

Open the Tailscale app from your menu bar and sign in.

**Windows:**

Download the installer from [tailscale.com/download/windows](https://tailscale.com/download/windows) and run it. Tailscale appears in the system tray — click it and sign in.

### Install on your phone

- **iOS** — [App Store](https://apps.apple.com/app/tailscale/id1470499037)
- **Android** — [Google Play](https://play.google.com/store/apps/details?id=com.tailscale.ipn)

Open the app and sign in with the **same account** you used on your computer. Toggle the connection on.

### Verify it works

On your computer, run:

```bash
tailscale status
```

You should see both your computer and your phone listed. That's it — your devices can now find each other.

## 2. Install be-conductor

**Linux / macOS:**

```bash
curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash
```

The installer checks for Python 3.10+, installs pipx if needed, downloads the latest release, and offers to set up autostart (systemd on Linux, launchd on macOS, Task Scheduler on Windows). If you accept, the server is started immediately — no need to run `be-conductor serve` separately.

**Windows** (PowerShell):

```powershell
irm https://github.com/somniacs/be-conductor/releases/latest/download/install.ps1 | iex
```

If the installer says Python is missing, grab it from [python.org](https://python.org) and run it again.

Restart your terminal after install if the `be-conductor` command is not found.

## 3. Start the server

```bash
be-conductor up
```

This starts the server in the background. The dashboard is now reachable at `http://127.0.0.1:7777`. Use `be-conductor serve` instead if you want foreground output (useful for debugging).

> **Tip:** You don't have to start the server manually every time — `be-conductor run` auto-starts it if it isn't running. But if you want the dashboard available before launching any agents, run `be-conductor up` first.

To start the server automatically on boot, see [Auto-Start on Boot](autostart.md). The autostart setup also starts the server right away, so you don't need to run `be-conductor up` separately.

## 4. Run an agent

```bash
be-conductor run claude research
```

Done. The agent is running. Start more if you want:

```bash
be-conductor run aider backend
be-conductor run codex feature
```

### Isolated worktree sessions

Run agents in their own git branch so they don't conflict with each other or your work:

```bash
be-conductor run -w claude refactor-auth
be-conductor run -w claude add-tests
```

Each gets its own branch and working copy. When done, merge from the dashboard or CLI:

```bash
be-conductor worktree merge refactor-auth --strategy squash
```

## 5. Open on your phone

```bash
be-conductor qr
```

Scan the QR code with your phone. The dashboard opens — all your sessions, live terminal, full control.

Or type the URL directly. Tailscale's [MagicDNS](https://tailscale.com/kb/1081/magicdns) lets you use your computer's name:

```
http://my-laptop:7777
```

Run `tailscale status` to see the name. No IP to remember.

## 6. IDE plugins (optional)

Run and manage sessions directly from your editor — no terminal needed.

- **VSCode** — install "be-conductor" from the [VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=somniacs.be-conductor-vscode)
- **JetBrains** — install "be-conductor" from the [JetBrains Marketplace](https://plugins.jetbrains.com/plugin/26768-be-conductor)

Both plugins auto-discover the running server and let you start, attach, resume, and observe sessions in an embedded terminal tab. Worktree isolation is available from the new-session dialog.

## 7. Keep it running

The be-conductor server starts automatically when you run your first agent and stays running in the background. If you accepted autostart during install, the dashboard is already reachable after a reboot. Otherwise, see [Auto-Start on Boot](autostart.md) for manual systemd (Linux), launchd (macOS), and Task Scheduler (Windows) setup.

## Quick reference

| Do this | Command |
|---|---|
| Start the server (background) | `be-conductor up` |
| Start the server (foreground) | `be-conductor serve` |
| Start an agent | `be-conductor run claude research` |
| Start in a worktree | `be-conductor run -w claude research` |
| Start in background | `be-conductor run -d claude research` |
| Resume a session | `be-conductor resume research` |
| List sessions | `be-conductor list` |
| Attach to a session | `be-conductor attach research` |
| Detach without stopping | `Ctrl+]` |
| Open dashboard | `be-conductor open` |
| QR code for phone | `be-conductor qr` |
| Stop a session | `be-conductor stop research` |
| List worktrees | `be-conductor worktree list` |
| Merge a worktree | `be-conductor worktree merge research` |
| Discard a worktree | `be-conductor worktree discard research` |
| Shut everything down | `be-conductor shutdown` |
| Show all commands | `be-conductor --help` |
