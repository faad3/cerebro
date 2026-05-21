"""In-memory hub of live WebSocket connections on this master instance.

Single-process state — for Phase 1 we run a single master node.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import WebSocket


@dataclass
class NodeConn:
    node_id: str
    ws: WebSocket
    # terminal_id -> Future resolved when the node confirms `terminal_created`
    pending_creates: Dict[str, asyncio.Future] = field(default_factory=dict)
    # request_id -> Future resolved when node sends `session_response`
    pending_reads: Dict[str, asyncio.Future] = field(default_factory=dict)

    async def send_json(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)


class Hub:
    def __init__(self) -> None:
        self._nodes: Dict[str, NodeConn] = {}
        # terminal_id -> browser WS (one viewer per terminal for Phase 1)
        self._viewers: Dict[str, WebSocket] = {}
        # terminal_id -> epoch of last PTY output (for activity tracking)
        self._activity: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ---- nodes ---------------------------------------------------------

    async def attach_node(self, node_id: str, ws: WebSocket) -> NodeConn:
        async with self._lock:
            existing = self._nodes.get(node_id)
            if existing is not None:
                try:
                    await existing.ws.close(code=4000)
                except Exception:
                    pass
            conn = NodeConn(node_id=node_id, ws=ws)
            self._nodes[node_id] = conn
            return conn

    async def detach_node(self, node_id: str, conn: NodeConn) -> bool:
        """Returns True if this was the active connection (removed from hub)."""
        async with self._lock:
            current = self._nodes.get(node_id)
            removed = current is conn
            if removed:
                self._nodes.pop(node_id, None)
            for fut in conn.pending_creates.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("node disconnected"))
            for fut in conn.pending_reads.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("node disconnected"))
            return removed

    def get_node(self, node_id: str) -> Optional[NodeConn]:
        return self._nodes.get(node_id)

    # ---- viewers (browser terminal windows) ----------------------------

    async def attach_viewer(self, terminal_id: str, ws: WebSocket) -> None:
        async with self._lock:
            existing = self._viewers.get(terminal_id)
            if existing is not None:
                try:
                    await existing.close(code=4001)
                except Exception:
                    pass
            self._viewers[terminal_id] = ws

    async def detach_viewer(self, terminal_id: str, ws: WebSocket) -> None:
        async with self._lock:
            current = self._viewers.get(terminal_id)
            if current is ws:
                self._viewers.pop(terminal_id, None)

    def get_viewer(self, terminal_id: str) -> Optional[WebSocket]:
        return self._viewers.get(terminal_id)

    # ---- activity tracking ---------------------------------------------

    def touch_activity(self, terminal_id: str) -> None:
        self._activity[terminal_id] = time.time()

    def get_activity(self, terminal_id: str) -> Optional[float]:
        return self._activity.get(terminal_id)

    def clear_activity(self, terminal_id: str) -> None:
        self._activity.pop(terminal_id, None)


hub = Hub()
