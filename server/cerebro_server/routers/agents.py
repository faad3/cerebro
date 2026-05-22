"""REST endpoints for agents — the logical Claude Code session abstraction.

An agent owns one mandatory `claude` terminal and an optional `bash` side
terminal. Lifecycle:
    - POST /api/agents          → spawn agent (creates claude terminal)
    - POST /api/agents/{id}/bash → attach a bash side-terminal
    - DELETE /api/agents/{id}/bash → detach (kill) the bash side-terminal
    - DELETE /api/agents/{id}   → kill the agent (and all its terminals)
"""

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import audit
from ..auth import require_bearer
from ..hub import hub
from pydantic import BaseModel as _BaseModel
from ..models import AgentInfo, CreateAgentRequest, RenameRequest, TerminalInfo, TerminalKind
from ..registry import Registry

logger = logging.getLogger("cerebro.agents")

router = APIRouter(prefix="/api/agents", tags=["agents"])

CREATE_TIMEOUT_SECONDS = 10.0


def get_registry() -> Registry:
    from .. import main; registry = main.registry

    return registry


async def _spawn_terminal(
    reg: Registry,
    agent_id: str,
    node_id: str,
    kind: TerminalKind,
    cols: int,
    rows: int,
    options: Optional[dict] = None,
) -> TerminalInfo:
    """Allocate a terminal id, send create_terminal to the node, await ack."""
    node_conn = hub.get_node(node_id)
    if node_conn is None:
        raise HTTPException(status_code=409, detail="node not connected")

    terminal_id = str(uuid.uuid4())
    info = await reg.create_terminal(terminal_id, agent_id, node_id, kind)

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    node_conn.pending_creates[terminal_id] = fut

    msg = {
        "type": "create_terminal",
        "terminal_id": terminal_id,
        "kind": kind,
        "cols": cols,
        "rows": rows,
    }
    if options:
        msg["options"] = options

    try:
        await node_conn.send_json(msg)
        result = await asyncio.wait_for(fut, timeout=CREATE_TIMEOUT_SECONDS)
        info = await reg.mark_terminal_status(
            terminal_id, "running", pid=result.get("pid")
        )
        assert info is not None
        return info
    except asyncio.TimeoutError:
        await reg.delete_terminal(terminal_id)
        raise HTTPException(status_code=504, detail="node did not confirm terminal")
    except Exception as exc:
        await reg.delete_terminal(terminal_id)
        raise HTTPException(status_code=502, detail=f"node error: {exc}")
    finally:
        node_conn.pending_creates.pop(terminal_id, None)


@router.post("", dependencies=[Depends(require_bearer)])
async def create_agent(body: CreateAgentRequest, request: Request) -> AgentInfo:
    reg = get_registry()
    if await reg.get_node(body.node_id) is None:
        raise HTTPException(status_code=404, detail="node not found")
    if hub.get_node(body.node_id) is None:
        raise HTTPException(status_code=409, detail="node not connected")

    from ..plugins_loader import get_plugin, fill_auto_fields, build_command

    plugin_id = body.plugin_id or "claude"
    plugin = get_plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"plugin '{plugin_id}' not found")

    # Merge plugin defaults + body.plugin_options + legacy convenience fields.
    options = {}
    for opt in plugin.get("options", []):
        if "default" in opt:
            options[opt["key"]] = opt["default"]
    options.update(body.plugin_options or {})
    # Legacy convenience: top-level fields → plugin_options (for "claude" plugin).
    if body.cwd is not None:
        options["cwd"] = body.cwd
    if body.dangerously_skip_permissions is not None:
        options["skip_perms"] = body.dangerously_skip_permissions
    if body.name and not options.get("name"):
        options["name"] = body.name
    # Auto-filled fields (uuid for session_id, etc).
    options = fill_auto_fields(plugin, options)

    agent_id = str(uuid.uuid4())
    name = body.name or options.get("name")
    if not name:
        from ..funny_names import random_name
        name = random_name()
        options["name"] = name

    # Determine cwd — most plugins have a cwd option.
    cwd = options.get("cwd")
    # Claude's session_id is what we use for resume.
    claude_session_id = options.get("session_id") if plugin_id == "claude" else None
    skip_perms = bool(options.get("skip_perms"))

    await reg.create_agent(
        agent_id=agent_id,
        node_id=body.node_id,
        name=name,
        template=body.template or plugin_id,
        claude_terminal_id=None,
        dangerously_skip_permissions=skip_perms,
    )
    info = await reg.get_agent(agent_id)
    assert info is not None
    info.plugin_id = plugin_id
    info.plugin_options = options
    info.claude_session_id = claude_session_id
    info.cwd = cwd
    info.folder_id = body.folder_id
    await reg.update_agent(info)

    # Build the actual shell command from plugin manifest.
    command = build_command(plugin, options)

    # Spawn options sent to node (plus legacy compat for older nodes).
    spawn_options: dict = {
        "command": command,
        "cwd": cwd,
        # legacy fallback for older nodes that don't read "command":
        "session_id": claude_session_id,
        "dangerously_skip_permissions": skip_perms,
    }

    try:
        term = await _spawn_terminal(
            reg, agent_id, body.node_id, "claude",  # kind kept for legacy node compat
            body.cols, body.rows, options=spawn_options,
        )
    except HTTPException:
        await reg.delete_agent(agent_id)
        raise

    info = await reg.get_agent(agent_id)
    assert info is not None
    info.claude_terminal_id = term.terminal_id
    await reg.update_agent(info)
    audit.log(
        "agent.create",
        request,
        agent_id=agent_id,
        plugin_id=plugin_id,
        node_id=body.node_id,
        name=name,
        cwd=cwd,
    )
    return info


def _enrich_activity(agents: list) -> None:
    """Stamp each agent's last_activity_at from the in-memory hub."""
    from datetime import timezone

    for a in agents:
        if a.claude_terminal_id:
            ts = hub.get_activity(a.claude_terminal_id)
            if ts is not None:
                from datetime import datetime as _dt

                a.last_activity_at = _dt.fromtimestamp(ts, tz=timezone.utc)


@router.get("", dependencies=[Depends(require_bearer)])
async def list_agents(
    node_id: Optional[str] = Query(default=None),
) -> list[AgentInfo]:
    reg = get_registry()
    agents = await reg.list_agents(node_id=node_id)
    _enrich_activity(agents)
    return agents


@router.get("/{agent_id}", dependencies=[Depends(require_bearer)])
async def get_agent(agent_id: str) -> AgentInfo:
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    _enrich_activity([info])
    return info


@router.patch("/{agent_id}", dependencies=[Depends(require_bearer)])
async def update_agent(agent_id: str, body: dict) -> AgentInfo:
    """Update agent fields: name, folder_id, is_favorite, position."""
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    prev_folder = info.folder_id
    if "name" in body:
        info.name = body["name"] or None
    if "is_favorite" in body:
        new_fav = bool(body["is_favorite"])
        if new_fav != info.is_favorite:
            # Section changed → drop out of any current folder so the agent can be re-foldered in the new section.
            info.folder_id = None
        info.is_favorite = new_fav
    if "folder_id" in body:
        # Validate: folder.section must match agent.is_favorite (default=non-fav, favorite=fav).
        new_folder_id = body["folder_id"]
        if new_folder_id is None:
            info.folder_id = None
        else:
            folder = await reg.get_folder(new_folder_id)
            if folder is None:
                raise HTTPException(status_code=404, detail="folder not found")
            target_fav = folder.section == "favorite"
            if target_fav != info.is_favorite:
                info.is_favorite = target_fav  # follow the folder's section
            info.folder_id = new_folder_id
    if "position" in body:
        info.position = int(body["position"])
    await reg.update_agent(info)
    # Auto-delete the previous folder if the agent left and no others remain.
    if prev_folder and prev_folder != info.folder_id:
        peers = [a for a in await reg.list_agents() if a.folder_id == prev_folder]
        if not peers:
            await reg.delete_folder(prev_folder)
    return info


@router.delete("/{agent_id}", dependencies=[Depends(require_bearer)])
async def delete_agent(agent_id: str, request: Request) -> dict:
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")

    node_conn = hub.get_node(info.node_id)
    for tid in await reg.terminals_for_agent(agent_id):
        if node_conn is not None:
            try:
                await node_conn.send_json(
                    {"type": "kill_terminal", "terminal_id": tid}
                )
            except Exception:
                pass
        await reg.delete_terminal(tid)

    folder_id = info.folder_id
    await reg.delete_agent(agent_id)
    if folder_id:
        peers = [a for a in await reg.list_agents() if a.folder_id == folder_id]
        if not peers:
            await reg.delete_folder(folder_id)
    audit.log(
        "agent.delete",
        request,
        agent_id=agent_id,
        plugin_id=info.plugin_id,
        node_id=info.node_id,
        name=info.name,
    )
    return {"ok": True}


@router.post("/{agent_id}/resume", dependencies=[Depends(require_bearer)])
async def resume_agent(
    agent_id: str,
    request: Request,
    cols: int = Query(default=120),
    rows: int = Query(default=40),
) -> AgentInfo:
    """Revive a dead/orphaned agent by resuming its claude session."""
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if info.status == "running":
        raise HTTPException(status_code=409, detail="agent is already running")
    if not info.claude_session_id:
        raise HTTPException(
            status_code=409,
            detail="no claude session id captured — cannot resume",
        )
    if hub.get_node(info.node_id) is None:
        raise HTTPException(status_code=409, detail="node not connected")

    # Clean up old terminal record if any.
    if info.claude_terminal_id:
        await reg.delete_terminal(info.claude_terminal_id)

    # Use --session-id (idempotent): resumes if session has history, starts
    # fresh with same id if not. Avoids "No conversation found" errors when
    # the agent was created but never used.
    options: dict = {"session_id": info.claude_session_id}
    if info.dangerously_skip_permissions:
        options["dangerously_skip_permissions"] = True
    if info.cwd:
        options["cwd"] = info.cwd

    try:
        claude_term = await _spawn_terminal(
            reg, agent_id, info.node_id, "claude", cols, rows, options=options
        )
    except HTTPException:
        raise

    info = await reg.get_agent(agent_id)
    assert info is not None
    info.claude_terminal_id = claude_term.terminal_id
    info.status = "running"
    await reg.update_agent(info)
    audit.log(
        "agent.resume",
        request,
        agent_id=agent_id,
        plugin_id=info.plugin_id,
        node_id=info.node_id,
        name=info.name,
        session_id=info.claude_session_id,
    )
    return info


@router.post("/{agent_id}/bash", dependencies=[Depends(require_bearer)])
async def attach_bash(
    agent_id: str,
    cols: int = Query(default=80),
    rows: int = Query(default=24),
) -> TerminalInfo:
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if info.bash_terminal_id is not None:
        existing = await reg.get_terminal(info.bash_terminal_id)
        if existing is not None and existing.status == "running":
            return existing
        # Stale; clear it and re-spawn.
        info.bash_terminal_id = None
        await reg.update_agent(info)

    term = await _spawn_terminal(reg, agent_id, info.node_id, "bash", cols, rows)
    info = await reg.get_agent(agent_id)
    assert info is not None
    info.bash_terminal_id = term.terminal_id
    await reg.update_agent(info)
    return term


@router.delete("/{agent_id}/bash", dependencies=[Depends(require_bearer)])
async def detach_bash(agent_id: str) -> dict:
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if info.bash_terminal_id is None:
        return {"ok": True}

    bash_id = info.bash_terminal_id
    node_conn = hub.get_node(info.node_id)
    if node_conn is not None:
        try:
            await node_conn.send_json(
                {"type": "kill_terminal", "terminal_id": bash_id}
            )
        except Exception:
            pass
    await reg.delete_terminal(bash_id)
    info.bash_terminal_id = None
    await reg.update_agent(info)
    return {"ok": True}


@router.get("/{agent_id}/messages", dependencies=[Depends(require_bearer)])
async def get_messages(agent_id: str, last: int = Query(default=50)):
    """Read the last N parsed messages from the agent's claude session JSONL."""
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if not info.claude_session_id:
        return []

    node_conn = hub.get_node(info.node_id)
    if node_conn is None:
        raise HTTPException(status_code=409, detail="node not connected")

    request_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    node_conn.pending_reads[request_id] = fut

    try:
        await node_conn.send_json(
            {
                "type": "read_session",
                "request_id": request_id,
                "session_id": info.claude_session_id,
                "last": min(last, 200),
            }
        )
        result = await asyncio.wait_for(fut, timeout=10.0)
        return result.get("messages", [])
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="node did not respond")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        node_conn.pending_reads.pop(request_id, None)


class InputRequest(_BaseModel):
    text: str


@router.post("/{agent_id}/input", dependencies=[Depends(require_bearer)])
async def send_input(agent_id: str, body: InputRequest) -> dict:
    """Type text into the agent's claude terminal (as if the user typed it)."""
    reg = get_registry()
    info = await reg.get_agent(agent_id)
    if info is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if not info.claude_terminal_id:
        raise HTTPException(status_code=409, detail="no claude terminal")

    node_conn = hub.get_node(info.node_id)
    if node_conn is None:
        raise HTTPException(status_code=409, detail="node not connected")

    from ..protocol import pack

    await node_conn.send_bytes(pack(info.claude_terminal_id, body.text.encode("utf-8")))
    return {"ok": True}
