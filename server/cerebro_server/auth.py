"""Bearer-token auth shared by HTTP and WebSocket entry points."""

import os
from typing import Optional

from fastapi import Header, HTTPException, status

CEREBRO_TOKEN = os.environ.get("CEREBRO_TOKEN", "")


def expected_token() -> str:
    return CEREBRO_TOKEN


def check_token(token: Optional[str]) -> bool:
    expected = expected_token()
    if not expected:
        # Fail-closed: refuse if no token is configured on the server.
        return False
    return token == expected


async def require_bearer(authorization: Optional[str] = Header(default=None)) -> None:
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    token = authorization.split(" ", 1)[1].strip()
    if not check_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        )
