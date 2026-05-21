"""GET /api/plugins — list available plugins."""

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_bearer
from ..plugins_loader import list_plugins, get_plugin

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("", dependencies=[Depends(require_bearer)])
def get_plugins() -> list:
    return list_plugins()


@router.get("/{plugin_id}", dependencies=[Depends(require_bearer)])
def get_plugin_by_id(plugin_id: str) -> dict:
    p = get_plugin(plugin_id)
    if not p:
        raise HTTPException(status_code=404, detail="plugin not found")
    return p
