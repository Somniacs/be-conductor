# be-conductor — Local orchestration for terminal sessions.
#
# Copyright (c) 2026 Max Rheiner / Somniacs AG
#
# Licensed under the MIT License. You may obtain a copy
# of the license at:
#
#     https://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

"""CLI commands for starting, stopping, attaching to, and managing sessions."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote as _urlquote

import click
import httpx

from be_conductor.utils.config import (
    CONDUCTOR_TOKEN, HOST, PORT, PID_FILE, VERSION, CERTS_DIR,
    get_base_url, ensure_dirs,
)
import be_conductor.utils.config as _cfg


def _http_kwargs() -> dict:
    """Extra kwargs for httpx calls (disables cert verification for self-signed)."""
    if _cfg.SSL_CERTFILE:
        return {"verify": False}
    return {}


def _auth_headers() -> dict[str, str]:
    """Return Authorization header if CONDUCTOR_TOKEN is set."""
    if CONDUCTOR_TOKEN:
        return {"Authorization": f"Bearer {CONDUCTOR_TOKEN}"}
    return {}


def server_running() -> bool:
    try:
        r = httpx.get(f"{get_base_url()}/health", timeout=2, **_http_kwargs())
        return r.status_code == 200
    except Exception:
        return False


def start_server_daemon() -> bool:
    ensure_dirs()
    log_path = Path.home() / ".be-conductor" / "logs" / "server.log"

    project_root = Path(__file__).parent.parent.resolve()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")

    log = log_path.open("a")
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(project_root),
        env=env,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    cmd = [sys.executable, "-m", "be_conductor.server.app"]
    proc = subprocess.Popen(cmd, **popen_kwargs)
    log.close()

    for i in range(20):
        time.sleep(0.25)
        if server_running():
            return True
        # Check if process died immediately
        ret = proc.poll()
        if ret is not None:
            click.echo(f"Server process exited with code {ret}.", err=True)
            try:
                tail = log_path.read_text().strip().split("\n")[-10:]
                for line in tail:
                    click.echo(f"  {line}", err=True)
            except Exception:
                pass
            return False

    click.echo("Server did not respond in time.", err=True)
    try:
        tail = log_path.read_text().strip().split("\n")[-10:]
        for line in tail:
            click.echo(f"  {line}", err=True)
    except Exception:
        pass
    return False


@click.group()
@click.version_option(VERSION, prog_name="be-conductor")
def cli():
    """be-conductor — Local orchestration for interactive terminal processes."""


@cli.command()
@click.option("--host", default=HOST, help="Host to bind to")
@click.option("--port", default=PORT, type=int, help="Port to bind to")
@click.option("--ssl-cert", default=None, type=click.Path(exists=True), help="Path to SSL certificate (PEM)")
@click.option("--ssl-key", default=None, type=click.Path(exists=True), help="Path to SSL private key (PEM)")
def serve(host, port, ssl_cert, ssl_key):
    """Start the be-conductor server."""
    from be_conductor.server.app import run_server

    scheme = "https" if (ssl_cert or _cfg.SSL_CERTFILE) else "http"
    click.echo(f"be-conductor server on {host}:{port}")
    click.echo(f"  Dashboard: {scheme}://{host}:{port}")
    run_server(host=host, port=port, ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)


def _get_local_ip() -> str:
    """Best-effort detection of the LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@cli.command()
@click.option("--days", default=365, type=int, help="Certificate validity in days")
def cert(days):
    """Generate a self-signed HTTPS certificate."""
    ensure_dirs()

    cert_path = CERTS_DIR / "cert.pem"
    key_path = CERTS_DIR / "key.pem"

    if cert_path.exists():
        if not click.confirm(f"Certificate already exists at {cert_path}. Overwrite?"):
            click.echo("Aborted.")
            return

    local_ip = _get_local_ip()
    san = f"DNS:localhost,IP:127.0.0.1,IP:{local_ip}"

    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", str(days),
                "-nodes",
                "-subj", "/CN=be-conductor",
                "-addext", f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        click.echo("Error: openssl is required but not found.", err=True)
        click.echo("Install it with: sudo apt install openssl  # or brew install openssl", err=True)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error generating certificate: {e.stderr}", err=True)
        sys.exit(1)

    key_path.chmod(0o600)

    # Update config
    _cfg.set_ssl_config(str(cert_path), str(key_path))

    click.echo(f"Certificate generated:")
    click.echo(f"  cert: {cert_path}")
    click.echo(f"  key:  {key_path}")
    click.echo(f"  SAN:  {san}")
    click.echo(f"  valid: {days} days")
    click.echo()
    click.echo("HTTPS is now configured. Restart the server for changes to take effect:")
    click.echo("  be-conductor restart -f")


def _get_latest_version() -> str | None:
    """Check GitHub for the latest release. Returns version string or None."""
    try:
        r = httpx.get(
            "https://api.github.com/repos/somniacs/be-conductor/releases/latest",
            timeout=3,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        latest = r.json().get("tag_name", "").lstrip("v")
        if not latest:
            return None
        current = VERSION.split(".")
        remote = latest.split(".")
        for a, b in zip(current, remote):
            if int(b) > int(a):
                return latest
            if int(a) > int(b):
                return None
        return None
    except Exception:
        return None


def _check_for_update(gui: bool = False):
    """Check for updates. Runs in a background thread so it never blocks startup."""
    import threading

    def _run():
        latest = _get_latest_version()
        if not latest:
            return
        click.echo(
            click.style(f"  Update available: v{VERSION} → v{latest}", fg="yellow")
            + f"\n  https://github.com/somniacs/be-conductor/releases/latest"
        )
        if gui:
            _show_update_dialog(latest)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Give the thread a moment to print the hint before the CLI exits,
    # but never block for more than 5 seconds.
    t.join(timeout=5)


def _show_update_dialog(latest: str):
    """Show a native desktop dialog offering to update. Runs in background."""
    title = "Be-Conductor Update"
    msg = f"A new version is available: v{VERSION} → v{latest}\\n\\nUpdate now?"

    if sys.platform == "win32":
        # PowerShell MessageBox — "Yes" triggers the install one-liner
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            f"$r = [System.Windows.Forms.MessageBox]::Show("
            f"'A new version of Be-Conductor is available: v{VERSION} → v{latest}.`n`nUpdate now?',"
            f"'{title}',"
            f"[System.Windows.Forms.MessageBoxButtons]::YesNo,"
            f"[System.Windows.Forms.MessageBoxIcon]::Information);"
            "if ($r -eq 'Yes') {"
            "  Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-Command',"
            "    \"irm https://github.com/somniacs/be-conductor/releases/latest/download/install.ps1 | iex\""
            "}"
        )
        try:
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    elif sys.platform == "darwin":
        # macOS: osascript dialog
        script = (
            f'set r to display dialog "A new version of Be-Conductor is available: '
            f'v{VERSION} → v{latest}.\\n\\nUpdate now?" '
            f'buttons {{"Later", "Update"}} default button "Update" '
            f'with title "{title}" with icon note\n'
            f'if button returned of r is "Update" then\n'
            f'  do shell script "curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash &"\n'
            f'end if'
        )
        try:
            subprocess.Popen(["osascript", "-e", script])
        except Exception:
            pass
    else:
        # Linux: try zenity (GNOME), then kdialog (KDE)
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        install_cmd = "curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash"
        # Find a terminal emulator for running the install script
        term = None
        for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
            if shutil.which(t):
                term = t
                break
        # gnome-terminal uses -- instead of -e
        term_flag = "--" if term and "gnome-terminal" in term else "-e"
        run_install = (
            f'{term} {term_flag} bash -c \'{install_cmd}; echo "\\nDone. Press Enter to close."; read\''
            if term else install_cmd
        )
        if zenity:
            try:
                subprocess.Popen([
                    "bash", "-c",
                    f'{zenity} --question --title "{title}" '
                    f'--text "A new version of Be-Conductor is available: v{VERSION} → v{latest}.\\n\\nUpdate now?" '
                    f'--ok-label "Update" --cancel-label "Later" --no-wrap '
                    f'&& {run_install} || true',
                ])
            except Exception:
                pass
        elif kdialog:
            try:
                subprocess.Popen([
                    "bash", "-c",
                    f'{kdialog} --title "{title}" '
                    f'--yesno "A new version of Be-Conductor is available: v{VERSION} → v{latest}.\\n\\nUpdate now?" '
                    f'&& {run_install} || true',
                ])
            except Exception:
                pass


@cli.command()
def up():
    """Start the be-conductor server in the background."""
    if server_running():
        try:
            r = httpx.get(f"{get_base_url()}/health", timeout=2, **_http_kwargs())
            version = r.json().get("version", "?")
        except Exception:
            version = "?"
        click.echo(f"Server already running (v{version}) on {get_base_url()}")
        _check_for_update(gui=True)
        return

    click.echo("Starting server...")
    if start_server_daemon():
        click.echo(f"Server started on {get_base_url()}")
        _check_for_update(gui=True)
    else:
        click.echo("Failed to start server. Try: be-conductor serve", err=True)
        sys.exit(1)


@cli.command()
@click.argument("command")
@click.argument("name", required=False)
@click.option("-d", "--detach", is_flag=True, help="Run in background (don't attach to terminal)")
@click.option("-w", "--worktree", is_flag=True, help="Create an isolated git worktree for this session")
@click.option("--json", "use_json", is_flag=True, help="Output JSON (implies --detach)")
@click.option("--rows", type=int, default=None, help="Terminal rows (auto-detected if omitted)")
@click.option("--cols", type=int, default=None, help="Terminal columns (auto-detected if omitted)")
def run(command, name, detach, worktree, use_json, rows, cols):
    """Run a command in a new be-conductor session.

    By default, attaches to the session so you see output in your terminal.
    Use -d/--detach to run in the background.
    Use -w/--worktree to create an isolated git worktree for the session.

    Usage: be-conductor run COMMAND [NAME]

    Examples:
        be-conductor run claude research
        be-conductor run -d claude coding
        be-conductor run -w claude feature-auth
        be-conductor run "python train.py" training
    """
    if use_json:
        detach = True

    if name is None:
        name = command.split()[0]

    # Validate git repo if --worktree is requested
    if worktree:
        import subprocess as _sp
        try:
            _sp.run(["git", "rev-parse", "--show-toplevel"],
                     capture_output=True, text=True, check=True, timeout=5)
        except Exception:
            if use_json:
                click.echo(json.dumps({"error": "Not a git repository (--worktree requires a git repo)"}))
            else:
                click.echo("Error: --worktree requires the current directory to be a git repository.", err=True)
            sys.exit(1)

    if not server_running():
        if not use_json:
            click.echo("Server not running. Starting daemon...")
        if not start_server_daemon():
            if use_json:
                click.echo(json.dumps({"error": "Failed to start server"}))
            else:
                click.echo("Failed to start server. Try: be-conductor serve", err=True)
            sys.exit(1)
        if not use_json:
            click.echo(f"Server started on {get_base_url()}")

    # Include terminal size so the PTY spawns at the correct dimensions
    # from the start — avoids a resize race where the agent renders its
    # startup screen at 80 cols before the CLI sends a resize.
    size = shutil.get_terminal_size()
    payload = {
        "name": name, "command": command, "cwd": os.getcwd(),
        "source": "cli",
        "rows": rows or size.lines,
        "cols": cols or size.columns,
    }
    if worktree:
        payload["worktree"] = True

    r = httpx.post(
        f"{get_base_url()}/sessions/run",
        json=payload,
        headers=_auth_headers(),
        timeout=10,
        **_http_kwargs(),
    )

    if r.status_code == 200:
        data = r.json()
        if use_json:
            click.echo(json.dumps(data, indent=2))
        elif detach:
            click.echo(f"Session '{data['name']}' started (pid: {data['pid']})")
            if data.get("worktree"):
                click.echo(f"Worktree: {data['worktree']['worktree_path']}")
                click.echo(f"Branch:   {data['worktree']['branch']}")
            click.echo(f"Dashboard: {get_base_url()}")
        else:
            if data.get("worktree"):
                click.echo(f"Session '{data['name']}' started in worktree.")
                click.echo(f"  Branch: {data['worktree']['branch']}")
                click.echo(f"  Path:   {data['worktree']['worktree_path']}")
            click.echo(f"Attaching... (Ctrl+] to stop)")
            _resize_session(data["name"])
            _attach_session(data["name"], stop_on_exit=True)
    elif r.status_code == 409:
        if use_json:
            click.echo(json.dumps({"error": f"Session '{name}' already exists"}))
        else:
            click.echo(f"Session '{name}' already exists.", err=True)
        sys.exit(1)
    else:
        if use_json:
            click.echo(json.dumps({"error": r.text}))
        else:
            click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("name")
def attach(name):
    """Attach to a running session.

    Connects your terminal to the session's output and input.
    Press Ctrl+] to detach without stopping the session.
    """
    if not server_running():
        click.echo("Server not running.", err=True)
        sys.exit(1)

    # Verify session exists
    r = httpx.get(f"{get_base_url()}/sessions", headers=_auth_headers(), timeout=5, **_http_kwargs())
    sessions = {s["name"]: s for s in r.json()}
    if name not in sessions:
        click.echo(f"Session '{name}' not found.", err=True)
        sys.exit(1)

    click.echo(f"Attaching to '{name}'... (Ctrl+] to detach)")
    _attach_session(name)


def _attach_session(session_name: str, stop_on_exit: bool = False):
    """Attach terminal to a session via WebSocket.

    If *stop_on_exit* is True (used by ``run``), the session is gracefully
    stopped when the CLI detaches for any reason (Ctrl+C, Ctrl+], terminal
    close, or the session process exiting).
    """
    if sys.platform == "win32":
        _attach_session_win(session_name)
    else:
        _attach_session_unix(session_name, stop_on_exit=stop_on_exit)

    if stop_on_exit:
        _stop_session_quietly(session_name)


def _ws_url(session_name: str, source: str | None = None,
            client_id: str | None = None) -> str:
    """Build the WebSocket URL, appending auth and client identity."""
    url = get_base_url().replace("http://", "ws://") + f"/sessions/{_urlquote(session_name, safe='')}/stream"
    params = []
    if CONDUCTOR_TOKEN:
        params.append(f"token={CONDUCTOR_TOKEN}")
    if source:
        params.append(f"source={source}")
    if client_id:
        params.append(f"client_id={client_id}")
    if params:
        url += "?" + "&".join(params)
    return url


_last_sent_size: tuple[int, int] = (0, 0)


def _resize_session(session_name: str, client_id: str | None = None):
    """Send the current host terminal size to the remote PTY session."""
    global _last_sent_size
    try:
        size = shutil.get_terminal_size()
        dims = (size.lines, size.columns)
        if dims == _last_sent_size:
            return
        _last_sent_size = dims
        body: dict = {"rows": size.lines, "cols": size.columns, "source": "cli"}
        if client_id:
            body["client_id"] = client_id
        httpx.post(
            f"{get_base_url()}/sessions/{_urlquote(session_name, safe='')}/resize",
            json=body,
            headers=_auth_headers(),
            timeout=3,
            **_http_kwargs(),
        )
    except Exception:
        pass


def _stop_session_quietly(session_name: str):
    """Send a graceful stop to the session, ignoring errors."""
    try:
        httpx.post(
            f"{get_base_url()}/sessions/{_urlquote(session_name, safe='')}/stop",
            json={"mode": "graceful"},
            headers=_auth_headers(),
            timeout=5,
            **_http_kwargs(),
        )
    except Exception:
        pass


def _attach_session_unix(session_name: str, stop_on_exit: bool = False):
    """Unix attach — raw terminal with select-based I/O."""
    import select
    import signal
    import termios
    import threading
    import tty
    import websockets.sync.client as ws_sync

    client_id = str(uuid.uuid4())
    ws_url = _ws_url(session_name, source="cli", client_id=client_id)

    stdin_fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(stdin_fd)
    stop = threading.Event()

    wake_r, wake_w = os.pipe()

    def ws_reader(ws):
        try:
            for message in ws:
                if isinstance(message, bytes) and message:
                    sys.stdout.buffer.write(message)
                    sys.stdout.buffer.flush()
                # Text messages are JSON control events (resize, notifications)
                # — not terminal data, so don't print them.
        except Exception:
            pass
        finally:
            stop.set()
            os.write(wake_w, b"\x00")

    # Sync terminal size on attach and on SIGWINCH (terminal resize).
    # The signal handler only writes to a pipe to wake select();
    # the actual HTTP resize call happens safely in the main loop.
    _resize_session(session_name, client_id=client_id)

    resize_r, resize_w = os.pipe()
    os.set_blocking(resize_w, False)

    old_sigwinch = signal.getsignal(signal.SIGWINCH)

    def on_winch(signum, frame):
        try:
            os.write(resize_w, b"R")
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_winch)

    # If we own the session, also stop it on SIGHUP (terminal closed).
    old_sighup = None
    if stop_on_exit:
        old_sighup = signal.getsignal(signal.SIGHUP)
        def on_hup(signum, frame):
            _stop_session_quietly(session_name)
            sys.exit(0)
        signal.signal(signal.SIGHUP, on_hup)

    try:
        tty.setraw(stdin_fd)
        ws = ws_sync.connect(ws_url)

        reader_thread = threading.Thread(target=ws_reader, args=(ws,), daemon=True)
        reader_thread.start()

        # Final resize sync — catches any terminal size changes that
        # happened between session creation and handler installation.
        _resize_session(session_name, client_id=client_id)

        try:
            while not stop.is_set():
                readable, _, _ = select.select(
                    [stdin_fd, wake_r, resize_r], [], [], 0.5)

                if wake_r in readable:
                    break

                if resize_r in readable:
                    os.read(resize_r, 64)  # drain
                    _resize_session(session_name, client_id=client_id)

                # Also poll size on every iteration as a fallback.
                _resize_session(session_name, client_id=client_id)

                if stdin_fd in readable:
                    data = os.read(stdin_fd, 1024)
                    if not data:
                        break
                    if b"\x1d" in data:  # Ctrl+]
                        break
                    try:
                        ws.send(data)
                    except Exception:
                        break
        finally:
            try:
                ws.close()
            except Exception:
                pass
            os.close(wake_r)
            os.close(wake_w)
            os.close(resize_r)
            os.close(resize_w)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGWINCH, old_sigwinch)
        if old_sighup is not None:
            signal.signal(signal.SIGHUP, old_sighup)
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        if stop_on_exit:
            click.echo("")  # newline after raw mode
        else:
            click.echo("\nDetached.")


def _attach_session_win(session_name: str):
    """Windows attach — msvcrt-based console I/O with threading."""
    import msvcrt
    import threading
    import websockets.sync.client as ws_sync

    client_id = str(uuid.uuid4())
    ws_url = _ws_url(session_name, source="cli", client_id=client_id)
    stop = threading.Event()

    def ws_reader(ws):
        try:
            for message in ws:
                if isinstance(message, bytes) and message:
                    sys.stdout.buffer.write(message)
                    sys.stdout.buffer.flush()
                # Text messages are JSON control events (resize, notifications)
                # — not terminal data, so don't print them.
        except Exception:
            pass
        finally:
            stop.set()

    try:
        ws = ws_sync.connect(ws_url)
        reader_thread = threading.Thread(target=ws_reader, args=(ws,), daemon=True)
        reader_thread.start()

        try:
            while not stop.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x1d":  # Ctrl+]
                        break
                    try:
                        ws.send(ch.encode("utf-8"))
                    except Exception:
                        break
                else:
                    stop.wait(timeout=0.05)
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        click.echo("\nDetached.")


@cli.command("list")
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
def list_sessions(use_json):
    """List all active sessions."""
    if not server_running():
        if use_json:
            click.echo("[]")
        else:
            click.echo("Server not running.", err=True)
        sys.exit(1)

    r = httpx.get(f"{get_base_url()}/sessions", headers=_auth_headers(), timeout=5, **_http_kwargs())
    sessions = r.json()

    if use_json:
        click.echo(json.dumps(sessions, indent=2))
        return

    if not sessions:
        click.echo("No sessions.")
        return

    click.echo(f"{'NAME':<20} {'STATUS':<10} {'PID':<10} {'COMMAND'}")
    click.echo("-" * 60)
    for s in sessions:
        click.echo(
            f"{s['name']:<20} {s['status']:<10} {str(s.get('pid', '?')):<10} {s.get('command', '')}"
        )


@cli.command()
@click.argument("name")
@click.option("-d", "--detach", is_flag=True, help="Resume in background (don't attach)")
@click.option("-t", "--token", default=None, help="External resume token (e.g. UUID from agent output)")
@click.option("-c", "--command", "cmd", default=None, help="Agent command (default: claude)")
def resume(name, detach, token, cmd):
    """Resume an exited session.

    Restarts a session that exited with a resume token (e.g. Claude Code's
    --resume <id>). Attaches to the new session by default.

    Use --token to resume an external session inside be-conductor:

        be-conductor resume my-session --token <UUID>
        be-conductor resume my-session --token <UUID> --command aider

    Press Ctrl+] to detach without stopping the session.
    """
    if not server_running():
        click.echo("Server not running. Starting daemon...")
        if not start_server_daemon():
            click.echo("Failed to start server. Try: be-conductor serve", err=True)
            sys.exit(1)
        click.echo(f"Server started on {get_base_url()}")

    if token:
        # External resume: create a new session with <command> <flag> <token>
        agent = cmd or "claude"
        # Look up resume_flag from server config
        flag = "--resume"
        try:
            cfg = httpx.get(f"{get_base_url()}/config", headers=_auth_headers(), timeout=5, **_http_kwargs()).json()
            for entry in cfg.get("allowed_commands", []):
                if entry.get("command", "").split()[0] == agent.split()[0]:
                    flag = entry.get("resume_flag", "--resume")
                    break
        except Exception:
            pass
        size = shutil.get_terminal_size()
        payload = {
            "name": name,
            "command": f"{agent} {flag} {token}",
            "cwd": os.getcwd(),
            "source": "cli",
            "rows": size.lines,
            "cols": size.columns,
        }
        r = httpx.post(
            f"{get_base_url()}/sessions/run",
            json=payload,
            headers=_auth_headers(),
            timeout=10,
            **_http_kwargs(),
        )
    else:
        size = shutil.get_terminal_size()
        r = httpx.post(
            f"{get_base_url()}/sessions/{_urlquote(name, safe='')}/resume",
            json={"rows": size.lines, "cols": size.columns},
            headers=_auth_headers(),
            timeout=10,
            **_http_kwargs(),
        )

    if r.status_code == 200:
        data = r.json()
        if detach:
            click.echo(f"Session '{data['name']}' resumed (pid: {data['pid']})")
        else:
            click.echo(f"Attaching... (Ctrl+] to detach)")
            _resize_session(data["name"])
            _attach_session(data["name"])
    elif r.status_code == 404:
        click.echo(f"Session '{name}' not found or not resumable.", err=True)
        sys.exit(1)
    else:
        detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        click.echo(f"Error: {detail}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("name")
def stop(name):
    """Stop a running session."""
    if not server_running():
        click.echo("Server not running.", err=True)
        sys.exit(1)

    r = httpx.delete(f"{get_base_url()}/sessions/{_urlquote(name, safe='')}", headers=_auth_headers(), timeout=5, **_http_kwargs())
    if r.status_code == 200:
        click.echo(f"Session '{name}' stopped.")
    elif r.status_code == 404:
        click.echo(f"Session '{name}' not found.", err=True)
        sys.exit(1)
    else:
        click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--json", "use_json", is_flag=True, help="Output JSON for agent consumption")
def status(use_json):
    """Show server status and connection info."""
    running = server_running()

    if use_json:
        info = {
            "ok": running,
            "version": None,
            "base_url": get_base_url(),
            "ws_base_url": get_base_url().replace("http://", "ws://"),
            "auth": {"mode": "bearer" if CONDUCTOR_TOKEN else "none"},
            "hostname": socket.gethostname(),
            "pid": None,
        }
        if running:
            try:
                r = httpx.get(f"{get_base_url()}/health", timeout=2, **_http_kwargs())
                health = r.json()
                info["version"] = health.get("version")
            except Exception:
                pass
            try:
                pid_text = PID_FILE.read_text().strip()
                info["pid"] = int(pid_text)
            except Exception:
                pass
        click.echo(json.dumps(info, indent=2))
        return

    if not running:
        click.echo("Server not running.")
        return

    try:
        r = httpx.get(f"{get_base_url()}/health", timeout=2, **_http_kwargs())
        health = r.json()
        version = health.get("version", "?")
    except Exception:
        version = "?"

    pid = None
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        pass

    click.echo(f"be-conductor v{version}")
    click.echo(f"  URL:  {get_base_url()}")
    click.echo(f"  Host: {socket.gethostname()}")
    if pid:
        click.echo(f"  PID:  {pid}")
    click.echo(f"  Auth: {'bearer token' if CONDUCTOR_TOKEN else 'none'}")
    _check_for_update()


def _find_server_pid() -> int | None:
    """Find the be-conductor server PID, trying PID file first, then process list."""
    # 1. Try PID file
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # Verify it's actually the be-conductor server
            os.kill(pid, 0)
            return pid
        except (ProcessLookupError, ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)

    # 2. Fall back to searching for the process
    if sys.platform == "win32":
        return None
    try:
        result = subprocess.run(
            ["pgrep", "-f", "be_conductor.server.app"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # May match multiple lines; take the first
            for line in result.stdout.strip().split("\n"):
                pid = int(line.strip())
                if pid != os.getpid():
                    return pid
    except Exception:
        pass
    return None


def _warn_active_sessions() -> bool:
    """Check for running sessions and prompt for confirmation.

    Returns True if the caller should proceed, False to abort.
    """
    try:
        r = httpx.get(f"{get_base_url()}/sessions", headers=_auth_headers(), timeout=5, **_http_kwargs())
        sessions = r.json()
    except Exception:
        return True  # Can't reach server — nothing to warn about

    running = [s for s in sessions if s.get("status") == "running"]
    if not running:
        return True

    count = len(running)
    click.echo(f"\n  ⚠ {count} active session{'s' if count != 1 else ''} will be killed:")
    for s in running:
        label = s.get("name", s.get("id", "?"))
        cmd = s.get("command", "")
        click.echo(f"    • {label} ({cmd})" if cmd else f"    • {label}")
    click.echo()
    return click.confirm("  Continue?", default=False)


def stop_server() -> bool:
    """Stop the server daemon. Returns True if it was stopped."""
    pid = _find_server_pid()
    if pid is None:
        return False

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return True
    except (ProcessLookupError, ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)

    return False


@cli.command()
@click.option("--force", "-f", is_flag=True, help="Skip active-session warning")
def shutdown(force):
    """Stop the be-conductor server and all sessions."""
    if not server_running():
        click.echo("Server not running.")
        return

    if not force and not _warn_active_sessions():
        click.echo("Aborted.")
        return

    click.echo("Shutting down server...")
    stop_server()
    for _ in range(20):
        time.sleep(0.25)
        if not server_running():
            click.echo("Server stopped.")
            return
    click.echo("Server may still be running. Check manually.", err=True)
    sys.exit(1)


@cli.command()
@click.option("--force", "-f", is_flag=True, help="Skip active-session warning")
def restart(force):
    """Restart the be-conductor server (kills all sessions)."""
    if not server_running():
        click.echo("Server not running. Starting...")
    else:
        if not force and not _warn_active_sessions():
            click.echo("Aborted.")
            return
        click.echo("Stopping server...")
        stop_server()
        # Wait for it to die
        for _ in range(20):
            time.sleep(0.25)
            if not server_running():
                break

    if start_server_daemon():
        click.echo(f"Server restarted on {get_base_url()}")
    else:
        click.echo("Failed to start server. Try: be-conductor serve", err=True)
        sys.exit(1)


@cli.command()
def open():
    """Open the be-conductor dashboard in the default browser."""
    import webbrowser

    if not server_running():
        click.echo("Server not running. Starting daemon...")
        if not start_server_daemon():
            click.echo("Failed to start server. Try: be-conductor serve", err=True)
            sys.exit(1)
        click.echo(f"Server started on {get_base_url()}")

    click.echo(f"Opening {get_base_url()}")
    webbrowser.open(get_base_url())


## ---------------------------------------------------------------------------
# Worktree subcommands
# ---------------------------------------------------------------------------

@cli.group("worktree")
def worktree_group():
    """Manage git worktrees for isolated agent sessions."""


@worktree_group.command("list")
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
def worktree_list(use_json):
    """List all managed worktrees."""
    if not server_running():
        if use_json:
            click.echo("[]")
        else:
            click.echo("Server not running.", err=True)
        sys.exit(1)

    r = httpx.get(f"{get_base_url()}/worktrees", headers=_auth_headers(), timeout=5, **_http_kwargs())
    worktrees = r.json()

    if use_json:
        click.echo(json.dumps(worktrees, indent=2))
        return

    if not worktrees:
        click.echo("No managed worktrees.")
        return

    click.echo(f"{'NAME':<20} {'STATUS':<12} {'BRANCH':<30} {'COMMITS':<8} {'PATH'}")
    click.echo("-" * 100)
    for wt in worktrees:
        click.echo(
            f"{wt['name']:<20} {wt['status']:<12} {wt['branch']:<30} "
            f"{wt.get('commits_ahead', 0):<8} {wt['worktree_path']}"
        )


@worktree_group.command("discard")
@click.argument("name")
@click.option("--force", "-f", is_flag=True, help="Force discard even if there are unmerged changes")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def worktree_discard(name, force, yes):
    """Discard a worktree and delete its branch."""
    if not server_running():
        click.echo("Server not running.", err=True)
        sys.exit(1)

    if not yes:
        click.echo(f"This will permanently delete the worktree for '{name}' and its branch.")
        if not click.confirm("Continue?"):
            click.echo("Aborted.")
            return

    r = httpx.delete(
        f"{get_base_url()}/worktrees/{name}",
        params={"force": str(force).lower()},
        headers=_auth_headers(),
        timeout=10,
        **_http_kwargs(),
    )
    if r.status_code == 200:
        click.echo(f"Worktree '{name}' discarded.")
    else:
        click.echo(f"Error: {r.json().get('detail', r.text)}", err=True)
        sys.exit(1)


@worktree_group.command("merge")
@click.argument("name")
@click.option("--strategy", "-s", type=click.Choice(["squash", "merge", "rebase"]),
              default="squash", help="Merge strategy (default: squash)")
@click.option("--message", "-m", default=None, help="Custom commit message")
@click.option("--preview", is_flag=True, help="Preview the merge without doing it")
def worktree_merge(name, strategy, message, preview):
    """Merge a worktree branch back into its base branch."""
    if not server_running():
        click.echo("Server not running.", err=True)
        sys.exit(1)

    if preview:
        r = httpx.post(
            f"{get_base_url()}/worktrees/{name}/merge/preview",
            headers=_auth_headers(),
            timeout=10,
            **_http_kwargs(),
        )
        if r.status_code != 200:
            click.echo(f"Error: {r.json().get('detail', r.text)}", err=True)
            sys.exit(1)

        data = r.json()
        click.echo(f"Merge preview for '{name}':")
        click.echo(f"  Can merge:      {data['can_merge']}")
        click.echo(f"  Commits ahead:  {data['commits_ahead']}")
        click.echo(f"  Commits behind: {data['commits_behind']}")
        if data.get("conflict_files"):
            click.echo(f"  Conflicts:      {len(data['conflict_files'])}")
            for f in data["conflict_files"]:
                click.echo(f"    - {f}")
        if data.get("changed_files"):
            click.echo(f"  Changed files:  {len(data['changed_files'])}")
            for f in data["changed_files"][:20]:
                click.echo(f"    {f['status']:>1} {f['path']}")
            if len(data["changed_files"]) > 20:
                click.echo(f"    ... and {len(data['changed_files']) - 20} more")
        if data.get("message"):
            click.echo(f"  {data['message']}")
        return

    payload = {"strategy": strategy}
    if message:
        payload["message"] = message

    r = httpx.post(
        f"{get_base_url()}/worktrees/{name}/merge",
        json=payload,
        headers=_auth_headers(),
        timeout=30,
        **_http_kwargs(),
    )
    data = r.json()

    if r.status_code == 200 and data.get("success"):
        click.echo(f"Merged '{name}' into {data['target_branch']} ({data['strategy']} strategy)")
        click.echo(f"  {data['commits_merged']} commit(s) merged")
        click.echo(f"  Worktree and branch cleaned up")
    else:
        click.echo(f"Merge failed: {data.get('message', 'Unknown error')}", err=True)
        if data.get("conflict_files"):
            click.echo("Conflicting files:")
            for f in data["conflict_files"]:
                click.echo(f"  - {f}")
        sys.exit(1)


@worktree_group.command("gc")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without doing it")
@click.option("--max-age", type=float, default=7.0, help="Remove worktrees older than N days (default: 7)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def worktree_gc(dry_run, max_age, yes):
    """Garbage-collect stale and orphaned worktrees."""
    if not server_running():
        click.echo("Server not running.", err=True)
        sys.exit(1)

    r = httpx.post(
        f"{get_base_url()}/worktrees/gc",
        json={"dry_run": dry_run or not yes, "max_age_days": max_age},
        headers=_auth_headers(),
        timeout=30,
        **_http_kwargs(),
    )
    if r.status_code != 200:
        click.echo(f"Error: {r.json().get('detail', r.text)}", err=True)
        sys.exit(1)

    actions = r.json()
    if not actions:
        click.echo("Nothing to clean up.")
        return

    for action in actions:
        click.echo(f"  {action['action']}: {action['name']} ({action['reason']})")

    if dry_run or not yes:
        click.echo(f"\n{len(actions)} worktree(s) would be removed. Use --yes to confirm.")
    else:
        click.echo(f"\n{len(actions)} worktree(s) cleaned up.")


@cli.command()
def qr():
    """Show a QR code to open the dashboard on your phone.

    Detects your Tailscale MagicDNS name (or IP) and generates a scannable QR code.
    Prints it in the terminal and opens a clean SVG image as fallback.
    """
    import shutil
    import tempfile
    import webbrowser

    import qrcode
    import qrcode.image.svg

    if not server_running():
        click.echo("Server not running. Starting daemon...")
        if not start_server_daemon():
            click.echo("Failed to start server. Try: be-conductor serve", err=True)
            sys.exit(1)
        click.echo(f"Server started on {get_base_url()}")

    # Try to get Tailscale MagicDNS name (stable across IP changes), fall back to IP
    tailscale_host = None
    if shutil.which("tailscale"):
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                import json as _json
                status = _json.loads(result.stdout)
                dns_name = status.get("Self", {}).get("DNSName", "").rstrip(".")
                if dns_name:
                    tailscale_host = dns_name
        except Exception:
            pass
        if not tailscale_host:
            try:
                result = subprocess.run(
                    ["tailscale", "ip", "-4"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    tailscale_host = result.stdout.strip().split("\n")[0]
            except Exception:
                pass

    if tailscale_host:
        url = f"http://{tailscale_host}:{PORT}"
    else:
        url = f"http://127.0.0.1:{PORT}"
        click.echo("Tailscale not found. Using localhost (won't work from other devices).")

    local_url = f"http://127.0.0.1:{PORT}"

    # Print ASCII in terminal
    click.echo(f"\n♭ be-conductor — scan to open on your phone\n")
    qr_obj = qrcode.QRCode(border=2)
    qr_obj.add_data(url)
    qr_obj.make(fit=True)
    qr_obj.print_ascii(invert=True)
    click.echo(f"\n  {url}")
    if url != local_url:
        click.echo(f"  {local_url}")
    click.echo()

    # Generate a clean SVG, wrap in HTML page, and open in browser
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    svg_path = os.path.join(tempfile.gettempdir(), "be-conductor-qr.svg")
    img.save(svg_path)

    svg_data = Path(svg_path).read_text()

    html_path = os.path.join(tempfile.gettempdir(), "be-conductor-qr.html")
    Path(html_path).write_text(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>♭ conductor — Link Device</title>
<style>
body {{ margin:0; min-height:100vh; display:flex; flex-direction:column;
       align-items:center; justify-content:center; background:#0a0a1a;
       color:#e0e0e0; font-family:Helvetica,Arial,sans-serif; }}
h1 {{ font-size:28px; color:#8080ff; margin:0 0 6px; font-weight:600; }}
.sub {{ font-size:14px; color:#808090; margin-bottom:30px; }}
.qr {{ background:#ffffff; padding:24px; border-radius:12px; display:inline-block; }}
.qr svg {{ width:300px; height:300px; display:block; }}
.url {{ font-size:16px; color:#a0a0d0; margin-top:24px;
        font-family:monospace; letter-spacing:0.5px; }}
</style></head><body>
<h1>♭ conductor</h1>
<p class="sub">Scan to open on another device</p>
<div class="qr">{svg_data}</div>
<p class="url"><a href="{url}" style="color:#a0a0d0">{url}</a></p>
{"" if url == local_url else f'<p class="url"><a href="{local_url}" style="color:#a0a0d0">{local_url}</a></p>'}
</body></html>""")

    file_url = f"file://{html_path}"
    click.echo(f"  QR page: {file_url}")
    webbrowser.open(file_url)
    click.echo("  (opened in browser — check your browser window)")


if __name__ == "__main__":
    cli()
