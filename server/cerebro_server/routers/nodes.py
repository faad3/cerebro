"""REST endpoints for nodes (hosts running the cerebro-node daemon)."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_bearer
from ..hub import hub
from ..models import NodeInfo, RegisterNodeRequest, RenameRequest
from ..registry import Registry

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def get_registry() -> Registry:
    from .. import main; registry = main.registry

    return registry


@router.post("/register", dependencies=[Depends(require_bearer)])
async def register(body: RegisterNodeRequest) -> dict:
    reg = get_registry()
    await reg.register_node(body.node_id, body.hostname)
    return {"ok": True}


@router.post("/{node_id}/heartbeat", dependencies=[Depends(require_bearer)])
async def heartbeat(node_id: str) -> dict:
    reg = get_registry()
    refreshed = await reg.heartbeat_node(node_id)
    return {"ok": refreshed}


@router.get("", dependencies=[Depends(require_bearer)])
async def list_nodes() -> list[NodeInfo]:
    reg = get_registry()
    return await reg.list_nodes()


@router.patch("/{node_id}", dependencies=[Depends(require_bearer)])
async def rename_node(node_id: str, body: RenameRequest) -> NodeInfo:
    from fastapi import HTTPException

    reg = get_registry()
    info = await reg.rename_node(node_id, body.name)
    if info is None:
        raise HTTPException(status_code=404, detail="node not found")
    return info


@router.get("/{node_id}/ls", dependencies=[Depends(require_bearer)])
async def list_directory(node_id: str, path: str = Query(default="~")):
    """List directories on a node — for the path browser UI."""
    node_conn = hub.get_node(node_id)
    if node_conn is None:
        raise HTTPException(status_code=409, detail="node not connected")

    request_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    node_conn.pending_reads[request_id] = fut

    try:
        await node_conn.send_json({
            "type": "list_dir",
            "request_id": request_id,
            "path": path,
        })
        result = await asyncio.wait_for(fut, timeout=5.0)
        return {"path": result.get("path", path), "entries": result.get("entries", [])}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="node timeout")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        node_conn.pending_reads.pop(request_id, None)
