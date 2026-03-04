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

"""Platform-aware PTY wrapper (Unix pty + Windows ConPTY via pywinpty)."""

import os
import shlex
import subprocess
import sys


_IS_WIN = sys.platform == "win32"


class BasePTYProcess:
    """Common interface for PTY wrappers on all platforms."""

    def __init__(self, command: str, cwd: str | None = None, env: dict | None = None):
        self.command = command
        self.cwd = cwd
        self.extra_env = env
        self.closed = False

    def spawn(self, rows: int = 24, cols: int = 80) -> None:
        raise NotImplementedError

    def read(self) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    def poll(self) -> int | None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        raise NotImplementedError

    # Unix PTY exposes a file descriptor for async I/O; Windows uses a
    # different mechanism (thread-based read loop).  This attribute is
    # only valid on Unix.
    master_fd: int = -1


# ---------------------------------------------------------------------------
# Unix implementation
# ---------------------------------------------------------------------------

if not _IS_WIN:
    import fcntl
    import pty
    import signal
    import struct
    import termios

    class UnixPTYProcess(BasePTYProcess):
        """Wraps a subprocess in a Unix pseudo-terminal."""

        def __init__(self, command: str, cwd: str | None = None, env: dict | None = None):
            super().__init__(command, cwd, env=env)
            self.master_fd: int = -1
            self.process: subprocess.Popen | None = None

        def spawn(self, rows: int = 24, cols: int = 80) -> None:
            master_fd, slave_fd = pty.openpty()

            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

            args = shlex.split(self.command)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            for key in list(env):
                if key.startswith("CLAUDE"):
                    del env[key]
            if self.extra_env:
                env.update(self.extra_env)

            self.process = subprocess.Popen(
                args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.cwd,
                preexec_fn=os.setsid,
                env=env,
            )

            os.close(slave_fd)
            self.master_fd = master_fd
            os.set_blocking(self.master_fd, False)

        def read(self) -> bytes:
            return os.read(self.master_fd, 65536)

        def write(self, data: bytes) -> None:
            if self.master_fd >= 0 and not self.closed:
                os.write(self.master_fd, data)

        def resize(self, rows: int, cols: int) -> None:
            if self.master_fd >= 0 and not self.closed:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

        def kill(self) -> None:
            """Send SIGTERM to the process group (non-blocking)."""
            if self.process and self.process.poll() is None:
                try:
                    pgid = os.getpgid(self.process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

        def interrupt_pg(self) -> None:
            """Send SIGINT to the process group (non-blocking)."""
            if self.process and self.process.poll() is None:
                try:
                    pgid = os.getpgid(self.process.pid)
                    os.killpg(pgid, signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass

        def poll(self) -> int | None:
            if self.process:
                return self.process.poll()
            return None

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.kill()

        @property
        def pid(self) -> int | None:
            return self.process.pid if self.process else None


# ---------------------------------------------------------------------------
# Windows implementation (ConPTY via pywinpty)
# ---------------------------------------------------------------------------

else:
    import re as _re
    from winpty import PTY as WinPTY  # type: ignore[import-not-found]

    # ConPTY leaks terminal query responses into the output stream.
    # Strip them so they don't show up as visible garbage.
    _CONPTY_LEAK_RE = _re.compile(
        rb"\x1b\[\??[\d;]*[cRn]"  # DA1, DA2, DSR, CPR responses
        rb"|\x1b\[>\d[\d;]*c"     # DA2 response (alternate form)
    )

    class WindowsPTYProcess(BasePTYProcess):
        """Wraps a subprocess in a Windows ConPTY pseudo-terminal."""

        def __init__(self, command: str, cwd: str | None = None, env: dict | None = None):
            super().__init__(command, cwd, env=env)
            self._pty: WinPTY | None = None
            self._pid: int | None = None

        def spawn(self, rows: int = 24, cols: int = 80) -> None:
            self._pty = WinPTY(cols, rows)

            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            for key in list(env):
                if key.startswith("CLAUDE"):
                    del env[key]
            if self.extra_env:
                env.update(self.extra_env)

            # pywinpty spawn() expects: appname (str), cmdline (str|None),
            # cwd (str|None), env (null-separated str|None)
            parts = self.command.split(None, 1)
            appname = parts[0]
            cmdline = parts[1] if len(parts) > 1 else None
            cwd = self.cwd or os.getcwd()
            env_str = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"
            self._pty.spawn(appname, cmdline=cmdline, cwd=cwd, env=env_str)

            # pywinpty doesn't directly expose PID in all versions;
            # we store it if available
            self._pid = getattr(self._pty, "pid", None)

        def read(self) -> bytes:
            if self._pty and not self.closed:
                data = self._pty.read()
                if isinstance(data, str):
                    data = data.encode("utf-8", errors="replace")
                if data:
                    data = _CONPTY_LEAK_RE.sub(b"", data)
                return data or b""
            return b""

        def write(self, data: bytes) -> None:
            if self._pty and not self.closed:
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                self._pty.write(data)

        def resize(self, rows: int, cols: int) -> None:
            if self._pty and not self.closed:
                self._pty.set_size(cols, rows)

        def kill(self) -> None:
            if self._pid:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._pid)],
                        capture_output=True,
                    )
                except Exception:
                    pass

        def poll(self) -> int | None:
            if self._pty:
                return None if self._pty.isalive() else 0
            return None

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                self.kill()
                self._pty = None

        @property
        def pid(self) -> int | None:
            return self._pid


# ---------------------------------------------------------------------------
# Factory — returns the right class for the current platform
# ---------------------------------------------------------------------------

def PTYProcess(command: str, cwd: str | None = None, env: dict | None = None) -> BasePTYProcess:
    """Create a platform-appropriate PTY wrapper."""
    if _IS_WIN:
        return WindowsPTYProcess(command, cwd=cwd, env=env)
    return UnixPTYProcess(command, cwd=cwd, env=env)
