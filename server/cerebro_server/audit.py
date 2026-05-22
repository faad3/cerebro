"""Append-only audit log of state-changing API actions.

Writes JSON lines to {CEREBRO_DATA_DIR or /data or ~/.cerebro}/audit.log.
Best-effort: never raises into the request handler.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Request

logger = logging.getLogger("cerebro.audit")

_path_cache: Optional[Path] = None


def _audit_path() -> Path:
    global _path_cache
    if _path_cache is not None:
        return _path_cache
    explicit = os.environ.get("CEREBRO_DATA_DIR")
    if explicit:
        base = Path(explicit)
    elif Path("/data").exists() and os.access("/data", os.W_OK):
        base = Path("/data")
    else:
        base = Path.home() / ".cerebro"
    base.mkdir(parents=True, exist_ok=True)
    _path_cache = base / "audit.log"
    return _path_cache


def _client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "internal"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown") or "unknown"


def log(action: str, request: Optional[Request] = None, **fields: Any) -> None:
    """Append one JSON line. Swallows all errors."""
    try:
        record = {
            "ts": time.time(),
            "action": action,
            "ip": _client_ip(request),
            "ua": (request.headers.get("user-agent") if request else None),
        }
        # Drop None values to keep lines compact.
        for k, v in fields.items():
            if v is not None:
                record[k] = v
        line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
        with _audit_path().open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.debug("audit log write failed: %s", exc)
