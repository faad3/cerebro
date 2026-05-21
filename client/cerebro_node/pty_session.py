"""PTY session: fork bash, pipe master fd to/from a per-session asyncio queue."""

import asyncio
import errno
import fcntl
import logging
import os
import pty
import shlex
import signal
import struct
import termios
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("cerebro.pty")


@dataclass
class ExitStatus:
    code: int
    signal: Optional[int] = None


class PTYSession:
    """One pseudo-terminal slave + its child process.

    The agent owns the queue: the read loop pushes bytes in, the agent's
    sender task drains it and frames them onto the master WebSocket.
    """

    def __init__(
        self,
        session_id: str,
        cols: int,
        rows: int,
        shell_cmd: str = "bash",
        cwd: Optional[str] = None,
        on_data: Optional[Callable[[bytes], Awaitable[None]]] = None,
        on_exit: Optional[Callable[[ExitStatus], Awaitable[None]]] = None,
    ):
        self.session_id = session_id
        self.cols = cols
        self.rows = rows
        self.shell_cmd = shell_cmd
        self.cwd = cwd
        self.pid: Optional[int] = None
        self.fd: Optional[int] = None
        self._on_data = on_data
        self._on_exit = on_exit
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reaped = False
        self._reap_task: Optional[asyncio.Task] = None

    def start(self) -> int:
        """Fork + exec the shell. Returns the child pid (parent only)."""
        pid, fd = pty.fork()
        if pid == 0:
            # Child: replace itself with the shell.
            try:
                env = os.environ.copy()
                env.setdefault("TERM", "xterm-256color")
                # The daemon may have been started by systemd/setsid with a stripped
                # PATH that does not include user-local install dirs. Augment so
                # tools like `claude` (~/.local/bin) and Homebrew binaries resolve.
                home = os.path.expanduser("~")
                extras = [
                    f"{home}/.local/bin",
                    f"{home}/.npm-global/bin",
                    f"{home}/.cargo/bin",
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                ]
                cur = env.get("PATH", "")
                parts = cur.split(":") if cur else []
                for p in extras:
                    if p and os.path.isdir(p) and p not in parts:
                        parts.insert(0, p)
                env["PATH"] = ":".join(parts)
                # Use specified cwd, or default to home directory.
                target_dir = self.cwd or home
                try:
                    os.chdir(target_dir)
                except OSError:
                    os.chdir(home)
                argv = shlex.split(self.shell_cmd)
                os.execvpe(argv[0], argv, env)
            except Exception as exc:  # pragma: no cover — child path
                os.write(2, f"cerebro: failed to exec shell: {exc}\n".encode())
                os._exit(127)

        # Parent.
        self.pid = pid
        self.fd = fd
        self._loop = asyncio.get_event_loop()

        # Non-blocking master fd so add_reader can drain without stalling.
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.resize(self.cols, self.rows)
        self._loop.add_reader(fd, self._on_readable)
        self._reap_task = self._loop.create_task(self._reaper())
        return pid

    # ---- input/output ----

    def _on_readable(self) -> None:
        assert self.fd is not None and self._loop is not None
        try:
            data = os.read(self.fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            # PTY closed (child exited).
            self._on_eof()
            return

        if not data:
            self._on_eof()
            return

        if self._on_data is not None:
            # Schedule the async send; we are in a sync callback.
            self._loop.create_task(self._on_data(data))

    def _on_eof(self) -> None:
        assert self._loop is not None and self.fd is not None
        try:
            self._loop.remove_reader(self.fd)
        except Exception:
            pass

    async def _reaper(self) -> None:
        """Wait for the child to exit, then notify the agent."""
        assert self.pid is not None
        try:
            while not self._reaped:
                await asyncio.sleep(0.5)
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                except ChildProcessError:
                    self._reaped = True
                    break
                if pid == 0:
                    continue
                self._reaped = True
                code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
                exit_status = ExitStatus(code=code, signal=sig)
                logger.info(
                    "session %s pid=%d exited code=%s signal=%s",
                    self.session_id, self.pid, code, sig,
                )
                if self._on_exit is not None:
                    await self._on_exit(exit_status)
                break
        finally:
            self._cleanup_fd()

    def _cleanup_fd(self) -> None:
        if self.fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self.fd)
            except Exception:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write(self, data: bytes) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, data)
        except OSError as exc:
            logger.warning("write to %s failed: %s", self.session_id, exc)

    def resize(self, cols: int, rows: int) -> None:
        if self.fd is None:
            return
        self.cols, self.rows = cols, rows
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError as exc:
            logger.warning("resize %s failed: %s", self.session_id, exc)

    async def kill(self, sig: int = signal.SIGTERM) -> None:
        if self.pid is None or self._reaped:
            return
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            pass
