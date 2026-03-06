# Windows Support

**Status: Supported** (Windows 10 Build 1809+ / Windows 11)

be-conductor runs natively on Windows using [ConPTY](https://devblogs.microsoft.com/commandline/windows-command-line-introducing-the-windows-pseudo-console-conpty/) (Windows Pseudo Console) via the [`pywinpty`](https://pypi.org/project/pywinpty/) library.

## Prerequisites

- **Windows 10 Build 1809+** or Windows 11 — ConPTY is required
- **Python 3.10+** — download from [python.org](https://python.org). Check "Add Python to PATH" during install
- **Git** — download from [git-scm.com](https://git-scm.com/download/win)

To check your Windows build: press `Win+R`, type `winver`, press Enter.

To check Python: open PowerShell and run `python --version` or `py --version`.

## Install

### One-line install (recommended)

```powershell
irm https://github.com/somniacs/be-conductor/releases/latest/download/install.ps1 | iex
```

The installer checks for Python 3.10+, installs [pipx](https://pipx.pypa.io/) if needed, downloads the latest release, and offers to set up autostart via Task Scheduler.

### From source

```powershell
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
powershell -ExecutionPolicy Bypass -File install.ps1
```

After install, the `be-conductor` command is available from any terminal. If the command is not found, restart your terminal.

### Uninstall

```powershell
irm https://github.com/somniacs/be-conductor/releases/latest/download/uninstall.ps1 | iex
```

This stops the server, removes the scheduled task, uninstalls the package, and asks whether to keep your data (`~\.be-conductor\`).

### Manual install (without install script)

```powershell
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Usage

Works the same as Linux/macOS:

```powershell
be-conductor run <agent> research
be-conductor run <agent> coding
be-conductor list
be-conductor open
```

Open `http://127.0.0.1:7777` in your browser for the dashboard.

### Attach / Detach

- `be-conductor run <agent> research` — starts and attaches (you see output in your terminal)
- Press `Ctrl+]` to detach without stopping the session
- `be-conductor attach research` — reattach later
- `be-conductor run -d <agent> coding` — start detached (background)

### Git worktree isolation

Run agents in isolated branches so they don't conflict:

```powershell
be-conductor run -w <agent> refactor-auth
be-conductor run -w <agent> add-tests
```

Each session gets its own branch and working copy. Merge from the dashboard (with diff preview and strategy picker) or the CLI:

```powershell
be-conductor worktree merge refactor-auth --strategy squash
```

Merging is non-destructive — resume the session, make more changes, and merge again. Delete the worktree when done.

### Session resume

When an agent exits with a resume token, be-conductor captures it. Resume from the dashboard (play button) or CLI:

```powershell
be-conductor resume research
```

## Remote access from another device

1. Install [Tailscale](https://tailscale.com/download/windows) on your Windows machine and your phone, tablet, or laptop
2. Sign in with the same account on all devices
3. Run `be-conductor qr` to get a scannable QR code — or open the dashboard's **Servers** dialog (hamburger menu → Servers) to discover Tailscale devices automatically

## Multi-machine setup

To monitor sessions from multiple machines (Windows, Linux, Mac — any mix):

1. Install and start be-conductor on each machine
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

## How it works on Windows

| Component | Implementation |
|-----------|---------------|
| PTY | ConPTY via `pywinpty` |
| Terminal sizing | `pywinpty` resize API |
| Process management | `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` |
| Async I/O | Thread-based reader with `call_soon_threadsafe()` |
| CLI attach | `msvcrt` for console I/O |

The `pywinpty` dependency is installed automatically (it's a conditional dependency that only activates on Windows).

## Troubleshooting

### "pywinpty not found" or import error

Make sure you have Python 3.10+ and pip is up to date:

```powershell
python -m pip install --upgrade pip
pip install pywinpty
```

### ConPTY not available

ConPTY requires Windows 10 Build 1809 or later. Check your build with `winver`. If you're on an older build, use the WSL workaround below.

### Terminal output looks garbled

Make sure you're using Windows Terminal, PowerShell, or cmd.exe. Some third-party terminals may not fully support ConPTY escape sequences.

## WSL alternative

If you prefer, you can also run be-conductor inside WSL (Windows Subsystem for Linux):

```bash
# Inside WSL
git clone https://github.com/somniacs/be-conductor.git
cd be-conductor
./install.sh
be-conductor run <agent> research
```

Then open `http://localhost:7777` in your Windows browser. WSL provides full Unix PTY support.
