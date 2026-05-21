"""Redis-backed registry for nodes, agents, terminals, and replay buffers.

Schema (one master per Redis instance for Phase 1):

    nodes:{node_id}                → JSON NodeInfo, TTL 30s, refreshed by heartbeat
    nodes:all                      → SET of node_ids

    agents:{agent_id}              → JSON AgentInfo
    agents:by_node:{node_id}       → SET of agent_ids

    terminals:{terminal_id}        → JSON TerminalInfo
    terminals:by_agent:{agent_id}  → SET of terminal_ids
    terminals:by_node:{node_id}    → SET of terminal_ids

    replay_buffer:{terminal_id}    → LIST of bytes (rolling, ~1 MB)
"""

import json
from datetime import datetime, timezone
from typing import List, Optional

import redis.asyncio as aioredis

from .models import AgentInfo, NodeInfo, TerminalInfo, TerminalKind

NODE_TTL_SECONDS = 30
REPLAY_MAX_CHUNKS = 256


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Registry:
    def __init__(self, url: str):
        self._url = url
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(self._url, decode_responses=False)
        await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    @property
    def r(self) -> aioredis.Redis:
        assert self._redis is not None, "Registry.connect() not called"
        return self._redis

    # ---- nodes ---------------------------------------------------------

    async def register_node(self, node_id: str, hostname: str) -> NodeInfo:
        now = _now()
        # User-set name lives in a separate TTL-less hash so it survives the
        # periodic expiry of the live `nodes:{id}` record.
        existing = await self.get_node(node_id)
        name_bytes = await self.r.hget("nodes:names", node_id)
        name = name_bytes.decode() if isinstance(name_bytes, bytes) else name_bytes
        info = NodeInfo(
            node_id=node_id,
            hostname=hostname,
            name=name or (existing.name if existing else None),
            registered_at=existing.registered_at if existing else now,
            last_heartbeat=now,
            status="online",
        )
        await self.r.set(f"nodes:{node_id}", info.model_dump_json(), ex=NODE_TTL_SECONDS)
        await self.r.sadd("nodes:all", node_id)
        return info

    async def heartbeat_node(self, node_id: str) -> bool:
        raw = await self.r.get(f"nodes:{node_id}")
        if raw is None:
            return False
        info = NodeInfo.model_validate_json(raw)
        info.last_heartbeat = _now()
        info.status = "online"
        await self.r.set(f"nodes:{node_id}", info.model_dump_json(), ex=NODE_TTL_SECONDS)
        return True

    async def get_node(self, node_id: str) -> Optional[NodeInfo]:
        raw = await self.r.get(f"nodes:{node_id}")
        if raw is None:
            return None
        return NodeInfo.model_validate_json(raw)

    async def list_nodes(self) -> List[NodeInfo]:
        node_ids = await self.r.smembers("nodes:all")
        out: List[NodeInfo] = []
        stale: List[bytes] = []
        for nid in node_ids:
            nid_s = nid.decode() if isinstance(nid, bytes) else nid
            raw = await self.r.get(f"nodes:{nid_s}")
            if raw is None:
                stale.append(nid)
                continue
            out.append(NodeInfo.model_validate_json(raw))
        if stale:
            await self.r.srem("nodes:all", *stale)
        out.sort(key=lambda n: n.hostname)
        return out

    async def rename_node(self, node_id: str, name: str) -> Optional[NodeInfo]:
        info = await self.get_node(node_id)
        if info is None:
            return None
        info.name = name or None
        await self.r.set(f"nodes:{node_id}", info.model_dump_json(), ex=NODE_TTL_SECONDS)
        # Persist name in TTL-less hash so it survives node TTL expiry.
        if name:
            await self.r.hset("nodes:names", node_id, name)
        else:
            await self.r.hdel("nodes:names", node_id)
        return info

    # ---- agents --------------------------------------------------------

    async def create_agent(
        self,
        agent_id: str,
        node_id: str,
        name: Optional[str],
        template: str,
        claude_terminal_id: Optional[str],
        dangerously_skip_permissions: bool = False,
    ) -> AgentInfo:
        info = AgentInfo(
            agent_id=agent_id,
            node_id=node_id,
            name=name,
            template=template,
            created_at=_now(),
            status="running",
            claude_terminal_id=claude_terminal_id,
            bash_terminal_id=None,
            dangerously_skip_permissions=dangerously_skip_permissions,
        )
        await self.r.set(f"agents:{agent_id}", info.model_dump_json())
        await self.r.sadd(f"agents:by_node:{node_id}", agent_id)
        await self.r.sadd("agents:all", agent_id)
        return info

    async def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        raw = await self.r.get(f"agents:{agent_id}")
        if raw is None:
            return None
        return AgentInfo.model_validate_json(raw)

    async def update_agent(self, info: AgentInfo) -> None:
        await self.r.set(f"agents:{info.agent_id}", info.model_dump_json())

    async def rename_agent(self, agent_id: str, name: str) -> Optional[AgentInfo]:
        info = await self.get_agent(agent_id)
        if info is None:
            return None
        info.name = name or None
        await self.update_agent(info)
        return info

    async def delete_agent(self, agent_id: str) -> Optional[AgentInfo]:
        info = await self.get_agent(agent_id)
        if info is None:
            return None
        await self.r.delete(f"agents:{agent_id}")
        await self.r.srem(f"agents:by_node:{info.node_id}", agent_id)
        await self.r.srem("agents:all", agent_id)
        return info

    async def list_agents(self, node_id: Optional[str] = None) -> List[AgentInfo]:
        if node_id is not None:
            ids = await self.r.smembers(f"agents:by_node:{node_id}")
        else:
            ids = await self.r.smembers("agents:all")
        out: List[AgentInfo] = []
        for aid in ids:
            aid_s = aid.decode() if isinstance(aid, bytes) else aid
            info = await self.get_agent(aid_s)
            if info is not None:
                out.append(info)
        out.sort(key=lambda a: a.created_at)
        return out

    async def agents_for_node(self, node_id: str) -> List[str]:
        ids = await self.r.smembers(f"agents:by_node:{node_id}")
        return [s.decode() if isinstance(s, bytes) else s for s in ids]

    # ---- terminals -----------------------------------------------------

    async def create_terminal(
        self,
        terminal_id: str,
        agent_id: str,
        node_id: str,
        kind: TerminalKind,
    ) -> TerminalInfo:
        info = TerminalInfo(
            terminal_id=terminal_id,
            agent_id=agent_id,
            node_id=node_id,
            kind=kind,
            created_at=_now(),
            status="running",
        )
        await self.r.set(f"terminals:{terminal_id}", info.model_dump_json())
        await self.r.sadd(f"terminals:by_agent:{agent_id}", terminal_id)
        await self.r.sadd(f"terminals:by_node:{node_id}", terminal_id)
        return info

    async def get_terminal(self, terminal_id: str) -> Optional[TerminalInfo]:
        raw = await self.r.get(f"terminals:{terminal_id}")
        if raw is None:
            return None
        return TerminalInfo.model_validate_json(raw)

    async def update_terminal(self, info: TerminalInfo) -> None:
        await self.r.set(f"terminals:{info.terminal_id}", info.model_dump_json())

    async def mark_terminal_status(
        self, terminal_id: str, status: str, pid: Optional[int] = None
    ) -> Optional[TerminalInfo]:
        info = await self.get_terminal(terminal_id)
        if info is None:
            return None
        info.status = status  # type: ignore[assignment]
        if pid is not None:
            info.pid = pid
        await self.update_terminal(info)
        return info

    async def delete_terminal(self, terminal_id: str) -> Optional[TerminalInfo]:
        info = await self.get_terminal(terminal_id)
        if info is None:
            return None
        await self.r.delete(f"terminals:{terminal_id}")
        await self.r.srem(f"terminals:by_agent:{info.agent_id}", terminal_id)
        await self.r.srem(f"terminals:by_node:{info.node_id}", terminal_id)
        await self.r.delete(f"replay_buffer:{terminal_id}")
        return info

    async def terminals_for_agent(self, agent_id: str) -> List[str]:
        ids = await self.r.smembers(f"terminals:by_agent:{agent_id}")
        return [s.decode() if isinstance(s, bytes) else s for s in ids]

    async def terminals_for_node(self, node_id: str) -> List[str]:
        ids = await self.r.smembers(f"terminals:by_node:{node_id}")
        return [s.decode() if isinstance(s, bytes) else s for s in ids]

    # ---- folders -------------------------------------------------------

    async def create_folder(self, folder_id: str, name: str, node_id: Optional[str] = None, position: int = 0, section: str = "default") -> "FolderInfo":
        from .models import FolderInfo
        info = FolderInfo(folder_id=folder_id, name=name, node_id=node_id, position=position, section=section)
        await self.r.set(f"folders:{folder_id}", info.model_dump_json())
        await self.r.sadd("folders:all", folder_id)
        return info

    async def get_folder(self, folder_id: str):
        from .models import FolderInfo
        raw = await self.r.get(f"folders:{folder_id}")
        if raw is None:
            return None
        return FolderInfo.model_validate_json(raw)

    async def update_folder(self, info) -> None:
        await self.r.set(f"folders:{info.folder_id}", info.model_dump_json())

    async def delete_folder(self, folder_id: str) -> bool:
        # Clear folder_id from any agents that reference it.
        for aid in await self.r.smembers("agents:all"):
            aid_s = aid.decode() if isinstance(aid, bytes) else aid
            ag = await self.get_agent(aid_s)
            if ag and ag.folder_id == folder_id:
                ag.folder_id = None
                await self.update_agent(ag)
        await self.r.delete(f"folders:{folder_id}")
        await self.r.srem("folders:all", folder_id)
        return True

    async def list_folders(self) -> list:
        from .models import FolderInfo
        ids = await self.r.smembers("folders:all")
        out = []
        for fid in ids:
            fid_s = fid.decode() if isinstance(fid, bytes) else fid
            raw = await self.r.get(f"folders:{fid_s}")
            if raw:
                out.append(FolderInfo.model_validate_json(raw))
        out.sort(key=lambda f: f.position)
        return out

    # ---- replay buffer -------------------------------------------------

    async def append_replay(self, terminal_id: str, data: bytes) -> None:
        key = f"replay_buffer:{terminal_id}"
        pipe = self.r.pipeline()
        pipe.rpush(key, data)
        pipe.ltrim(key, -REPLAY_MAX_CHUNKS, -1)
        await pipe.execute()

    async def read_replay(self, terminal_id: str) -> List[bytes]:
        chunks = await self.r.lrange(f"replay_buffer:{terminal_id}", 0, -1)
        return list(chunks)
