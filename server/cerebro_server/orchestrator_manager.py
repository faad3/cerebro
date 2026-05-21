"""Orchestrator: a special claude PTY that lives inside the master container.

Unlike regular agents (managed by nodes), the orchestrator is spawned directly
by the master process. It runs `claude` with a CLAUDE.md that describes its
role + has `cerebro-ctl` in PATH for managing other agents.

The browser connects to it via WS /ws/orchestrator.
"""

import asyncio
import errno
import fcntl
import json
import logging
import os
import pty
import shlex
import signal
import struct
import termios
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("cerebro.orchestrator")

ORCHESTRATOR_DIR = Path(__file__).parent / "orchestrator"


class OrchestratorPTY:
    """Single orchestrator PTY instance managed by the master."""

    def __init__(self):
        self.pid: Optional[int] = None
        self.fd: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._viewer: Optional[WebSocket] = None
        self._output_buffer: list[bytes] = []
        self._max_buffer_chunks = 256
        self._started = False

    @property
    def alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def start(self, cols: int = 120, rows: int = 40) -> int:
        if self.alive:
            return self.pid

        pid, fd = pty.fork()
        if pid == 0:
            # Child: drop to non-root user 'cerebro', then exec claude.
            try:
                # Detect the UID/GID from the mounted .claude directory so we
                # can read credentials regardless of what host user owns them.
                claude_home = Path("/home/cerebro")
                claude_dir = claude_home / ".claude"
                if claude_dir.exists():
                    st = os.stat(str(claude_dir))
                    target_uid, target_gid = st.st_uid, st.st_gid
                    home = str(claude_home)
                else:
                    # Fallback: run as current user (dev outside Docker).
                    target_uid = os.getuid()
                    target_gid = os.getgid()
                    home = os.path.expanduser("~")

                if os.getuid() == 0 and target_uid != 0:
                    os.setgid(target_gid)
                    os.setuid(target_uid)

                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["HOME"] = home
                env["USER"] = "cerebro"

                # claude will pick up the CLAUDE.md from cwd.
                os.chdir(str(ORCHESTRATOR_DIR))
                cmd = "claude --dangerously-skip-permissions"
                argv = shlex.split(cmd)
                os.execvpe(argv[0], argv, env)
            except Exception as exc:
                os.write(2, f"orchestrator: exec failed: {exc}\n".encode())
                os._exit(127)

        # Parent.
        self.pid = pid
        self.fd = fd
        self._loop = asyncio.get_event_loop()
        self._started = True

        # Non-blocking fd.
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.resize(cols, rows)
        self._loop.add_reader(fd, self._on_readable)

        # Reaper task.
        asyncio.ensure_future(self._reaper())

        logger.info("orchestrator started pid=%d", pid)

        # Auto-trust the workspace by sending "1\n" after a brief delay
        # (claude shows "1. Yes, I trust this folder" on first launch).
        asyncio.ensure_future(self._auto_trust())

        return pid

    async def _auto_trust(self):
        """Wait for the trust prompt, then send '1' + Enter."""
        await asyncio.sleep(3)
        if self.alive and self.fd is not None:
            self.write(b"1\n")
            logger.info("orchestrator: sent auto-trust")

    def _on_readable(self):
        try:
            data = os.read(self.fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            self._on_eof()
            return
        if not data:
            self._on_eof()
            return
        # Buffer for replay.
        self._output_buffer.append(data)
        if len(self._output_buffer) > self._max_buffer_chunks:
            self._output_buffer = self._output_buffer[-self._max_buffer_chunks :]
        # Forward to viewer if connected.
        if self._viewer is not None and self._loop is not None:
            self._loop.create_task(self._send_to_viewer(data))

    async def _send_to_viewer(self, data: bytes):
        if self._viewer is None:
            return
        try:
            await self._viewer.send_bytes(data)
        except Exception:
            self._viewer = None

    def _on_eof(self):
        if self._loop and self.fd is not None:
            try:
                self._loop.remove_reader(self.fd)
            except Exception:
                pass

    async def _reaper(self):
        while self.alive:
            await asyncio.sleep(1)
        logger.info("orchestrator pid=%s exited", self.pid)
        self._cleanup()

    def _cleanup(self):
        if self.fd is not None:
            if self._loop:
                try:
                    self._loop.remove_reader(self.fd)
                except Exception:
                    pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self._started = False

    def write(self, data: bytes):
        if self.fd is None:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int):
        if self.fd is None:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    async def attach_viewer(self, ws: WebSocket):
        """Connect a browser WS as the viewer. Sends replay buffer."""
        self._viewer = ws
        for chunk in self._output_buffer:
            try:
                await ws.send_bytes(chunk)
            except Exception:
                break

    def detach_viewer(self, ws: WebSocket):
        if self._viewer is ws:
            self._viewer = None

    def kill(self):
        if self.pid and self.alive:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


# Singleton — one orchestrator per master process.
orchestrator = OrchestratorPTY()
