"""REST endpoints for folders (sidebar grouping)."""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_bearer
from ..models import CreateFolderRequest, FolderInfo, RenameRequest

router = APIRouter(prefix="/api/folders", tags=["folders"])


def get_registry():
    from .. import main
    return main.registry


@router.get("", dependencies=[Depends(require_bearer)])
async def list_folders() -> list[FolderInfo]:
    return await get_registry().list_folders()


@router.post("", dependencies=[Depends(require_bearer)])
async def create_folder(body: CreateFolderRequest) -> FolderInfo:
    reg = get_registry()
    fid = str(uuid.uuid4())
    existing = await reg.list_folders()
    pos = max((f.position for f in existing), default=-1) + 1
    return await reg.create_folder(fid, body.name, body.node_id, pos, body.section)


@router.patch("/{folder_id}", dependencies=[Depends(require_bearer)])
async def update_folder(folder_id: str, body: dict) -> FolderInfo:
    reg = get_registry()
    info = await reg.get_folder(folder_id)
    if info is None:
        raise HTTPException(status_code=404, detail="folder not found")
    if "name" in body:
        info.name = body["name"]
    if "section" in body and body["section"] in ("default", "favorite"):
        info.section = body["section"]
    if "position" in body:
        info.position = int(body["position"])
    await reg.update_folder(info)
    return info


@router.delete("/{folder_id}", dependencies=[Depends(require_bearer)])
async def delete_folder(folder_id: str) -> dict:
    await get_registry().delete_folder(folder_id)
    return {"ok": True}
