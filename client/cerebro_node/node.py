"""Cerebro node daemon: registers with the master, holds an internal WebSocket,
and manages PTY-backed terminals (claude or bash) on this host.
"""

import asyncio
import json
import logging
import os
import socket
import uuid
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

import httpx
import websockets

from .protocol import pack, unpack
from .pty_session import ExitStatus, PTYSession

logger = logging.getLogger("cerebro.node")

HEARTBEAT_INTERVAL = 10  # seconds
RECONNECT_BACKOFF_MIN = 1
RECONNECT_BACKOFF_MAX = 30


def _node_id_path() -> Path:
    home = Path(os.environ.get("CEREBRO_HOME", str(Path.home() / ".cerebro")))
    home.mkdir(parents=True, exist_ok=True)
    return home / "node_id"


def load_or_generate_node_id() -> str:
    p = _node_id_path()
    if p.exists():
        return p.read_text().strip()
    nid = str(uuid.uuid4())
    p.write_text(nid)
    return nid


def _ws_url(master_url: str, node_id: str, token: str) -> str:
    u = urlparse(master_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    path = f"/ws/node/{node_id}"
    return urlunparse((scheme, u.netloc, path, "", f"token={token}", ""))


class Node:
    """The cerebro-node daemon."""

    def __init__(
        self,
        master_url: str,
        token: str,
        claude_cmd: str = "claude",
        bash_cmd: str = "bash",
    ):
        self.master_url = master_url.rstrip("/")
        self.token = token
        self.claude_cmd = claude_cmd
        self.bash_cmd = bash_cmd
        self.node_id = load_or_generate_node_id()
        self.hostname = socket.gethostname()
        self.terminals: Dict[str, PTYSession] = {}
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    # ---- HTTP helpers --------------------------------------------------

    async def _register(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            f"{self.master_url}/api/nodes/register",
            json={"node_id": self.node_id, "hostname": self.hostname},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10.0,
        )
        r.raise_for_status()

    async def _send_heartbeat(self, client: httpx.AsyncClient) -> bool:
        r = await client.post(
            f"{self.master_url}/api/nodes/{self.node_id}/heartbeat",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10.0,
        )
        if r.status_code == 401:
            raise RuntimeError("invalid token")
        if r.status_code >= 400:
            return False
        return bool(r.json().get("ok"))

    async def _heartbeat_loop(self, client: httpx.AsyncClient) -> None:
        while not self._stop.is_set():
            try:
                ok = await self._send_heartbeat(client)
                if not ok:
                    logger.info("master lost our registration; re-registering")
                    await self._register(client)
            except Exception as exc:
                logger.warning("heartbeat failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass

    # ---- WS sending (serialized) ---------------------------------------

    async def _send_pty_data(self, terminal_id: str, data: bytes) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            try:
                await self._ws.send(pack(terminal_id, data))
            except Exception as exc:
                logger.debug("ws send failed: %s", exc)

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            try:
                await self._ws.send(json.dumps(payload))
            except Exception as exc:
                logger.debug("ws send_json failed: %s", exc)

    # ---- terminal lifecycle --------------------------------------------

    def _shell_for_kind(self, kind: str, options: Optional[dict] = None) -> str:
        options = options or {}
        # Plugin system: master sends a fully-rendered command string.
        if options.get("command"):
            return options["command"]
        # Legacy: build claude/bash command from kind+options (pre-plugins).
        if kind == "claude":
            cmd = self.claude_cmd
            if options.get("resume"):
                cmd = cmd + f" --resume {options['resume']}"
            elif options.get("session_id"):
                cmd = cmd + f" --session-id {options['session_id']}"
            if options.get("dangerously_skip_permissions"):
                cmd = cmd + " --dangerously-skip-permissions"
            return cmd
        if kind == "bash":
            return self.bash_cmd
        raise ValueError(f"unknown terminal kind: {kind!r}")

    def _make_data_callback(self, terminal_id: str):
        async def cb(data: bytes) -> None:
            await self._send_pty_data(terminal_id, data)

        return cb

    def _make_exit_callback(self, terminal_id: str):
        async def cb(status: ExitStatus) -> None:
            await self._send_json(
                {
                    "type": "terminal_dead",
                    "terminal_id": terminal_id,
                    "exit_code": status.code,
                    "signal": status.signal,
                }
            )
            self.terminals.pop(terminal_id, None)

        return cb

    def _kill_stale_claude(self, session_id: str) -> int:
        """Kill any existing claude process holding this session_id (orphaned
        from a previous node run). Returns count killed."""
        import signal as _sig, subprocess

        try:
            r = subprocess.run(
                ["pgrep", "-f", f"claude.*--session-id {session_id}"],
                capture_output=True, text=True, timeout=2,
            )
            pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        except Exception:
            return 0

        killed = 0
        for pid in pids:
            try:
                os.kill(pid, _sig.SIGTERM)
                killed += 1
            except ProcessLookupError:
                pass
        if killed:
            logger.info("killed %d stale claude(s) holding session %s", killed, session_id[:8])
            import time as _t
            _t.sleep(0.5)  # give SIGTERM a moment before we spawn replacement
        return killed

    async def _create_terminal(
        self,
        terminal_id: str,
        kind: str,
        cols: int,
        rows: int,
        options: Optional[dict] = None,
    ) -> None:
        if terminal_id in self.terminals:
            logger.warning("terminal %s already exists, ignoring create", terminal_id)
            return

        # If we're starting claude with a specific session_id, kill any orphan
        # claude already holding that session (left over from a previous run).
        if kind == "claude" and options:
            sid = options.get("session_id") or options.get("resume")
            if sid:
                self._kill_stale_claude(sid)

        try:
            shell_cmd = self._shell_for_kind(kind, options)
        except ValueError as exc:
            await self._send_json(
                {
                    "type": "terminal_dead",
                    "terminal_id": terminal_id,
                    "exit_code": -1,
                    "signal": None,
                    "error": str(exc),
                }
            )
            return

        sess = PTYSession(
            session_id=terminal_id,
            cols=cols,
            rows=rows,
            shell_cmd=shell_cmd,
            cwd=(options or {}).get("cwd"),
            on_data=self._make_data_callback(terminal_id),
            on_exit=self._make_exit_callback(terminal_id),
        )
        try:
            pid = sess.start()
        except Exception as exc:
            logger.exception("failed to start terminal %s", terminal_id)
            await self._send_json(
                {
                    "type": "terminal_dead",
                    "terminal_id": terminal_id,
                    "exit_code": -1,
                    "signal": None,
                    "error": str(exc),
                }
            )
            return
        self.terminals[terminal_id] = sess
        logger.info("terminal %s (%s) started pid=%d", terminal_id, kind, pid)
        await self._send_json(
            {"type": "terminal_created", "terminal_id": terminal_id, "pid": pid}
        )

    async def _kill_terminal(self, terminal_id: str) -> None:
        sess = self.terminals.get(terminal_id)
        if sess is None:
            return
        await sess.kill()

    async def _resize_terminal(self, terminal_id: str, cols: int, rows: int) -> None:
        sess = self.terminals.get(terminal_id)
        if sess is None:
            return
        sess.resize(cols, rows)

    # ---- WS receive ----------------------------------------------------

    async def _handle_message(self, msg) -> None:
        if isinstance(msg, (bytes, bytearray)):
            try:
                terminal_id, data = unpack(bytes(msg))
            except ValueError:
                logger.warning("malformed binary frame from master")
                return
            sess = self.terminals.get(terminal_id)
            if sess is not None:
                sess.write(data)
            return

        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            logger.warning("invalid JSON from master")
            return

        mtype = payload.get("type")
        if mtype == "create_terminal":
            await self._create_terminal(
                payload["terminal_id"],
                payload.get("kind", "bash"),
                int(payload.get("cols", 80)),
                int(payload.get("rows", 24)),
                options=payload.get("options"),
            )
        elif mtype == "kill_terminal":
            await self._kill_terminal(payload["terminal_id"])
        elif mtype == "resize":
            await self._resize_terminal(
                payload["terminal_id"],
                int(payload.get("cols", 80)),
                int(payload.get("rows", 24)),
            )
        elif mtype == "list_dir":
            await self._list_dir(
                payload.get("request_id", ""),
                payload.get("path", "~"),
            )
        elif mtype == "read_session":
            await self._read_session(
                payload.get("request_id", ""),
                payload.get("session_id", ""),
                int(payload.get("last", 50)),
            )
        else:
            logger.debug("unknown control message: %s", payload)

    async def _list_dir(self, request_id: str, path: str) -> None:
        """List directories at a given path for the path browser UI."""
        target = os.path.expanduser(path) if path else os.path.expanduser("~")
        entries = []
        try:
            for entry in sorted(os.scandir(target), key=lambda e: e.name):
                if entry.name.startswith("."):
                    continue
                try:
                    is_link = entry.is_symlink()
                    if entry.is_dir(follow_symlinks=True):
                        entries.append({
                            "name": entry.name,
                            "type": "dir",
                            "symlink": is_link,
                        })
                except (PermissionError, OSError):
                    pass
        except PermissionError:
            pass
        except FileNotFoundError:
            pass

        await self._send_json({
            "type": "list_dir_response",
            "request_id": request_id,
            "path": target,
            "entries": entries[:200],
        })

    async def _read_session(
        self, request_id: str, session_id: str, last: int
    ) -> None:
        """Read the last N entries from a claude session's JSONL file."""
        # Session files live at ~/.claude/projects/<cwd-encoded>/<session_id>.jsonl
        # Our agents start in $HOME, so cwd-encoded is like "-home-<user>".
        import glob as _glob

        home = Path.home()
        pattern = str(home / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
        matches = _glob.glob(pattern)
        messages = []
        if matches:
            try:
                with open(matches[0], "r") as f:
                    lines = f.readlines()
                for line in lines[-last:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        mtype = obj.get("type", "")
                        if mtype in ("user", "assistant", "system"):
                            entry = {
                                "type": mtype,
                                "uuid": obj.get("uuid"),
                                "timestamp": obj.get("timestamp"),
                            }
                            msg = obj.get("message", {})
                            if mtype == "user":
                                content = msg.get("content", "")
                                if isinstance(content, str):
                                    entry["text"] = content[:500]
                                elif isinstance(content, list):
                                    # tool_result — extract text
                                    parts = []
                                    for c in content:
                                        if isinstance(c, dict):
                                            if c.get("type") == "tool_result":
                                                parts.append(
                                                    f"[tool_result: {str(c.get('content', ''))[:200]}]"
                                                )
                                            elif c.get("type") == "text":
                                                parts.append(c.get("text", "")[:300])
                                    entry["text"] = " ".join(parts)[:500]
                            elif mtype == "assistant":
                                parts = []
                                for c in msg.get("content", []):
                                    if isinstance(c, dict):
                                        if c.get("type") == "text":
                                            parts.append(c.get("text", "")[:500])
                                        elif c.get("type") == "tool_use":
                                            parts.append(
                                                f"[tool: {c.get('name', '?')}]"
                                            )
                                entry["text"] = " ".join(parts)[:1000]
                                entry["model"] = msg.get("model")
                                entry["stop_reason"] = msg.get("stop_reason")
                                usage = msg.get("usage", {})
                                entry["tokens"] = {
                                    "input": usage.get("input_tokens", 0),
                                    "output": usage.get("output_tokens", 0),
                                    "cache_read": usage.get(
                                        "cache_read_input_tokens", 0
                                    ),
                                    "cache_create": usage.get(
                                        "cache_creation_input_tokens", 0
                                    ),
                                }
                            elif mtype == "system":
                                entry["subtype"] = obj.get("subtype")
                                entry["durationMs"] = obj.get("durationMs")
                            messages.append(entry)
                    except json.JSONDecodeError:
                        continue
            except Exception as exc:
                logger.warning("read_session failed: %s", exc)

        await self._send_json(
            {
                "type": "session_response",
                "request_id": request_id,
                "session_id": session_id,
                "messages": messages,
            }
        )

    async def _ws_loop(self) -> None:
        url = _ws_url(self.master_url, self.node_id, self.token)
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, max_size=None
        ) as ws:
            self._ws = ws
            logger.info("connected to master %s", self.master_url)

            # Re-register existing terminals so master knows about them
            # after a reconnect (e.g., after the old WS was kicked).
            for tid, sess in self.terminals.items():
                if sess.pid is not None:
                    await self._send_json({
                        "type": "terminal_created",
                        "terminal_id": tid,
                        "pid": sess.pid,
                    })

            try:
                async for msg in ws:
                    await self._handle_message(msg)
            finally:
                self._ws = None
                # DON'T kill terminals on WS disconnect — they survive
                # reconnects. Only kill on node shutdown (in run()'s finally).

    # ---- top-level run with reconnect/backoff --------------------------

    async def run(self) -> None:
        async with httpx.AsyncClient() as client:
            await self._register(client)
            logger.info("registered as %s (%s)", self.node_id, self.hostname)

            hb_task = asyncio.create_task(self._heartbeat_loop(client))
            backoff = RECONNECT_BACKOFF_MIN

            try:
                while not self._stop.is_set():
                    try:
                        await self._ws_loop()
                        backoff = RECONNECT_BACKOFF_MIN
                    except Exception as exc:
                        logger.warning(
                            "ws loop ended: %s; reconnecting in %ds", exc, backoff
                        )
                        try:
                            await asyncio.wait_for(self._stop.wait(), backoff)
                        except asyncio.TimeoutError:
                            pass
                        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
                        try:
                            await self._register(client)
                        except Exception as re_exc:
                            logger.warning("re-register failed: %s", re_exc)
            finally:
                self._stop.set()
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass
                # Kill terminals only on actual node shutdown.
                for sess in list(self.terminals.values()):
                    await sess.kill()
                self.terminals.clear()

    def stop(self) -> None:
        self._stop.set()
