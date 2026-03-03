# Quick Start

Control your AI agents from your phone in 5 minutes.

## 1. Set up Tailscale

Tailscale is a free app that creates a private network between your devices. Install it once, and your phone can reach your computer from anywhere.

1. Go to [tailscale.com](https://tailscale.com/) and create a free account
2. Install Tailscale on your computer and sign in
3. Install the Tailscale app on your phone ([iOS](https://apps.apple.com/app/tailscale/id1470499037) / [Android](https://play.google.com/store/apps/details?id=com.tailscale.ipn)) and sign in with the **same account**

That's it. Your devices can now find each other.

## 2. Install Be-Conductor

**Linux / macOS:**

```bash
curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash
```

The installer checks for Python 3.10+, installs pipx if needed, downloads the latest release, and offers to set up autostart (systemd on Linux, launchd on macOS).

**Windows** (PowerShell):

```powershell
irm https://github.com/somniacs/be-conductor/releases/latest/download/install.ps1 | iex
```

If the installer says Python is missing, grab it from [python.org](https://python.org) and run it again.

Restart your terminal after install if the `be-conductor` command is not found.

## 3. Run an agent

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

## 4. Open on your phone

```bash
be-conductor qr
```

Scan the QR code with your phone. The dashboard opens — all your sessions, live terminal, full control.

Or type the URL directly. Tailscale's [MagicDNS](https://tailscale.com/kb/1081/magicdns) lets you use your computer's name:

```
http://my-laptop:7777
```

Run `tailscale status` to see the name. No IP to remember.

## 5. Keep it running

The Be-Conductor server starts automatically when you run your first agent and stays running in the background. If you accepted autostart during install, the dashboard is already reachable after a reboot. Otherwise, see [Auto-Start on Boot](autostart.md) for manual systemd (Linux), launchd (macOS), and Task Scheduler (Windows) setup.

## Quick reference

| Do this | Command |
|---|---|
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
