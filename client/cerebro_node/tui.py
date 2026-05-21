"""cerebro-node tui — terminal UI for switching between Cerebro agents.

Flow:
  1. Show agent picker (list of agents with status)
  2. Select one → raw terminal mode, WS bridge to agent's PTY
  3. Ctrl+] → detach back to picker
  4. 'q' in picker → quit
  5. 'n' in picker → create new agent
  6. 'k' in picker → kill selected agent
"""

import asyncio
import json
import os
import signal
import struct
import sys
import termios
import tty
from typing import Optional

import httpx
import websockets


class CerebroTUI:
    def __init__(self, master_url: str, token: str):
        self.master_url = master_url.rstrip("/")
        self.token = token
        self._headers = {"Authorization": f"Bearer {token}"}
        self._running = True
        self._old_settings = None

    # ---- API helpers ---------------------------------------------------

    def _api_get(self, path: str):
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{self.master_url}{path}", headers=self._headers)
            r.raise_for_status()
            return r.json()

    def _api_post(self, path: str, body: dict = None):
        with httpx.Client(timeout=15) as c:
            r = c.post(f"{self.master_url}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            return r.json()

    def _api_delete(self, path: str):
        with httpx.Client(timeout=10) as c:
            r = c.delete(f"{self.master_url}{path}", headers=self._headers)
            r.raise_for_status()
            return r.json()

    # ---- Terminal helpers ----------------------------------------------

    def _clear(self):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    def _get_terminal_size(self):
        try:
            cols, rows = os.get_terminal_size()
            return cols, rows
        except OSError:
            return 80, 24

    # ---- Picker --------------------------------------------------------

    def _show_picker(self):
        """Show agent list with arrow-key navigation. Enter to attach."""
        cursor = 0

        while True:
            agents = self._api_get("/api/agents")
            nodes = {n["node_id"]: n for n in self._api_get("/api/nodes")}

            if cursor >= len(agents):
                cursor = max(0, len(agents) - 1)

            self._draw_picker(agents, nodes, cursor)

            key = self._read_key()

            if key == "q":
                return None
            elif key == "r":
                continue
            elif key == "UP":
                cursor = max(0, cursor - 1)
            elif key == "DOWN":
                cursor = min(len(agents) - 1, cursor + 1) if agents else 0
            elif key == "ENTER":
                if agents and 0 <= cursor < len(agents):
                    result = self._try_open_agent(agents[cursor])
                    if result:
                        return result
            elif key == "n":
                self._create_agent_interactive(nodes)
            elif key == "k":
                if agents and 0 <= cursor < len(agents):
                    a = agents[cursor]
                    try:
                        self._api_delete(f"/api/agents/{a['agent_id']}")
                    except Exception:
                        pass

    def _draw_picker(self, agents, nodes, cursor):
        self._clear()
        sys.stdout.write("\033[1;32m cerebro \033[0m")
        sys.stdout.write(f"\033[90m {len(agents)} agent(s)  •  {self.master_url}\033[0m\n")
        sys.stdout.write("\033[90m" + "─" * 60 + "\033[0m\n\n")

        if not agents:
            sys.stdout.write("  \033[90mno agents — press 'n' to create one\033[0m\n\n")
        else:
            for i, a in enumerate(agents):
                node = nodes.get(a["node_id"], {})
                hostname = node.get("name") or node.get("hostname", "?")[:15]
                name = a.get("name") or a["agent_id"][:10]
                status = a.get("status", "?")

                dot = {"running": "\033[32m●\033[0m", "dead": "\033[31m●\033[0m",
                       "orphaned": "\033[31m●\033[0m"}.get(status, "\033[33m●\033[0m")

                selected = i == cursor
                if selected:
                    sys.stdout.write(f"  \033[7m {dot} {name:20s} {hostname:16s} {status:10s} \033[0m\n")
                else:
                    sys.stdout.write(f"   {dot} {name:20s} \033[90m{hostname:16s} {status:10s}\033[0m\n")

        sys.stdout.write(f"\n\033[90m  ↑↓ navigate  ⏎ attach  n new  k kill  r refresh  q quit\033[0m\n")
        sys.stdout.flush()

    def _try_open_agent(self, agent):
        if agent["status"] == "running" and agent.get("claude_terminal_id"):
            return agent
        if agent.get("claude_session_id") and agent["status"] != "running":
            sys.stdout.write(f"\n  resuming {agent.get('name') or agent['agent_id'][:8]}...")
            sys.stdout.flush()
            try:
                cols, rows = self._get_terminal_size()
                self._api_post(f"/api/agents/{agent['agent_id']}/resume?cols={cols}&rows={rows}")
                import time; time.sleep(1)
                return self._api_get(f"/api/agents/{agent['agent_id']}")
            except Exception as e:
                sys.stdout.write(f" failed: {e}\n")
                import time; time.sleep(1)
        return None

    def _read_key(self) -> str:
        """Read a single keypress. Returns 'UP', 'DOWN', 'ENTER', or the char."""
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            ch = sys.stdin.read(1)
            return ch if ch else "q"
        try:
            tty.setcbreak(fd)
            ch = os.read(fd, 1)
            if not ch:
                return "q"
            if ch == b"\r" or ch == b"\n":
                return "ENTER"
            if ch == b"\x1b":
                # Escape sequence — read more with a short timeout so we
                # don't block forever if it was just a bare Escape press.
                import select as _sel
                buf = b""
                for _ in range(4):
                    r, _, _ = _sel.select([fd], [], [], 0.05)
                    if not r:
                        break
                    buf += os.read(fd, 1)
                # Arrow keys: \x1b[A/B or \x1bOA/B (application mode)
                if buf in (b"[A", b"OA"):
                    return "UP"
                if buf in (b"[B", b"OB"):
                    return "DOWN"
                if buf in (b"[C", b"OC"):
                    return "RIGHT"
                if buf in (b"[D", b"OD"):
                    return "LEFT"
                return "ESC"
            return ch.decode("utf-8", errors="replace")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _create_agent_interactive(self, nodes: dict):
        node_list = list(nodes.values())
        if not node_list:
            sys.stdout.write("\n  \033[31mno nodes available\033[0m\n")
            import time; time.sleep(1)
            return

        cursor = 0
        while True:
            self._clear()
            sys.stdout.write("\033[1;32m new agent \033[0m\033[90m  select node\033[0m\n")
            sys.stdout.write("\033[90m" + "─" * 50 + "\033[0m\n\n")
            for i, n in enumerate(node_list):
                h = n.get("name") or n["hostname"]
                status = n.get("status", "?")
                if i == cursor:
                    sys.stdout.write(f"  \033[7m  {h:25s} {status:8s}  \033[0m\n")
                else:
                    sys.stdout.write(f"    {h:25s} \033[90m{status:8s}\033[0m\n")
            sys.stdout.write(f"\n\033[90m  ↑↓ select  ⏎ create  Esc cancel\033[0m\n")
            sys.stdout.flush()

            key = self._read_key()
            if key == "UP":
                cursor = max(0, cursor - 1)
            elif key == "DOWN":
                cursor = min(len(node_list) - 1, cursor + 1)
            elif key == "ENTER":
                break
            elif key == "ESC" or key == "q":
                return

        node = node_list[cursor]
        node_id = node["node_id"]
        cols, rows = self._get_terminal_size()
        sys.stdout.write(f"\n  creating on {node.get('name') or node['hostname']}...")
        sys.stdout.flush()
        try:
            a = self._api_post("/api/agents", {
                "node_id": node_id,
                "cols": cols,
                "rows": rows,
                "dangerously_skip_permissions": True,
            })
            sys.stdout.write(f" ok ({a['agent_id'][:8]})\n")
        except Exception as e:
            sys.stdout.write(f" failed: {e}\n")
        import time
        time.sleep(1)

    def _kill_agent_interactive(self, agents: list):
        if not agents:
            return
        sys.stdout.write("\n  kill which? [1-9] > ")
        sys.stdout.flush()
        ch = self._read_key()
        if not ch.isdigit():
            return
        idx = int(ch) - 1
        if 0 <= idx < len(agents):
            a = agents[idx]
            try:
                self._api_delete(f"/api/agents/{a['agent_id']}")
                sys.stdout.write(f"  killed {a.get('name') or a['agent_id'][:8]}\n")
            except Exception as e:
                sys.stdout.write(f"  error: {e}\n")
            import time
            time.sleep(0.5)

    # ---- Attach (raw terminal ↔ WS bridge) ----------------------------

    async def _attach(self, agent: dict):
        """Bridge local terminal ↔ agent's PTY via WebSocket."""
        terminal_id = agent["claude_terminal_id"]
        ws_scheme = "wss" if self.master_url.startswith("https") else "ws"
        host = self.master_url.split("://", 1)[1]
        url = f"{ws_scheme}://{host}/ws/terminal/{terminal_id}?token={self.token}"

        cols, rows = self._get_terminal_size()
        fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(fd)
        out_fd = sys.stdout.fileno()

        name = agent.get("name") or agent["agent_id"][:8]
        sys.stdout.write(f"\033[32m[attached: {name}]\033[0m  Ctrl+] to detach\r\n")
        sys.stdout.flush()

        detach = False

        try:
            tty.setraw(fd)

            async with websockets.connect(
                url, max_size=None, ping_interval=20, ping_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

                loop = asyncio.get_event_loop()

                def on_winch(*_):
                    c, r = self._get_terminal_size()
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(
                            ws.send(json.dumps({"type": "resize", "cols": c, "rows": r}))
                        )
                    )

                signal.signal(signal.SIGWINCH, on_winch)

                async def read_stdin():
                    nonlocal detach
                    while True:
                        try:
                            data = await loop.run_in_executor(None, os.read, fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        if b"\x1d" in data:
                            detach = True
                            return
                        try:
                            await ws.send(data)
                        except Exception:
                            return

                async def read_ws():
                    try:
                        async for msg in ws:
                            if isinstance(msg, (bytes, bytearray)):
                                os.write(out_fd, msg)
                            elif isinstance(msg, str):
                                try:
                                    p = json.loads(msg)
                                    if p.get("type") == "terminal_dead":
                                        os.write(out_fd, b"\r\n\033[33m[terminal exited]\033[0m\r\n")
                                        return
                                except json.JSONDecodeError:
                                    pass
                    except websockets.ConnectionClosed:
                        os.write(out_fd, b"\r\n\033[31m[disconnected]\033[0m\r\n")
                    except Exception:
                        os.write(out_fd, b"\r\n\033[31m[ws error]\033[0m\r\n")

                stdin_task = asyncio.create_task(read_stdin())
                ws_task = asyncio.create_task(read_ws())

                await asyncio.wait(
                    [stdin_task, ws_task], return_when=asyncio.FIRST_COMPLETED
                )
                for t in [stdin_task, ws_task]:
                    if not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

        except websockets.exceptions.InvalidStatusCode as e:
            os.write(out_fd, f"\r\n\033[31m[ws rejected: {e}]\033[0m\r\n".encode())
            import time; time.sleep(2)
        except Exception as e:
            os.write(out_fd, f"\r\n\033[31m[error: {e}]\033[0m\r\n".encode())
            import time; time.sleep(2)
        finally:
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            termios.tcsetattr(fd, termios.TCSADRAIN, self._old_settings)

        return detach

    # ---- Main loop -----------------------------------------------------

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            while self._running:
                agent = self._show_picker()
                if agent is None:
                    break

                try:
                    detached = self._loop.run_until_complete(self._attach(agent))
                except Exception as exc:
                    # Restore terminal on crash so the user gets their shell back.
                    if self._old_settings:
                        try:
                            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
                        except Exception:
                            pass
                    sys.stdout.write(f"\r\n\033[31m[attach error: {exc}]\033[0m\r\n")
                    sys.stdout.flush()
                    import time; time.sleep(2)
        finally:
            self._loop.close()

        self._clear()
        sys.stdout.write("bye\n")
