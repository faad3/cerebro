"""Bearer-token auth shared by HTTP and WebSocket entry points.

Browser flow:
  POST /api/login {token: ...}  → sets httpOnly cookie `cerebro_session`
  All subsequent HTTP requests carry the cookie automatically.
  WS upgrade carries the cookie too (browsers send cookies on same-origin upgrades).
  POST /api/logout  → clears the cookie.

Node / CLI / orchestrator flow (unchanged):
  Authorization: Bearer <token>  (HTTP)
  ?token=<token>                 (WS query string)

Defense layers (this module):
  - constant-time token comparison
  - per-IP login rate limit (5 fails / 60s → 429)
  - optional Origin allowlist for WS upgrades (CEREBRO_ALLOWED_ORIGINS)
"""

import hmac
import logging
import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Header, HTTPException, Request, Response, WebSocket, status

logger = logging.getLogger("cerebro.auth")

COOKIE_NAME = "cerebro_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Rate-limit state (in-memory; resets on master restart).
_LOGIN_FAILS: Dict[str, Deque[float]] = defaultdict(deque)
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILS = 5


def expected_token() -> str:
    return os.environ.get("CEREBRO_TOKEN", "")


def check_token(token: Optional[str]) -> bool:
    expected = expected_token()
    if not expected or not token:
        return False
    # Constant-time compare to avoid timing-leak token recovery.
    return hmac.compare_digest(token, expected)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown") or "unknown"


def _login_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _LOGIN_FAILS[ip]
    while bucket and bucket[0] < now - LOGIN_WINDOW_SECONDS:
        bucket.popleft()
    return len(bucket) >= LOGIN_MAX_FAILS


def _record_login_fail(ip: str) -> None:
    _LOGIN_FAILS[ip].append(time.monotonic())


def _record_login_success(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)


def _is_secure_request(request: Request) -> bool:
    # Trust X-Forwarded-Proto from a reverse proxy that we control.
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, value: str, secure: bool) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=value,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


def _check_origin_allowed(origin: Optional[str]) -> bool:
    """Origin allowlist for WS upgrades (env-driven, off by default).

    CEREBRO_ALLOWED_ORIGINS="https://cerebro.example.com,http://localhost:8000"

    If env is empty/unset, no check is performed (suitable for trusted LAN setups).
    """
    allowed = os.environ.get("CEREBRO_ALLOWED_ORIGINS", "").strip()
    if not allowed:
        return True
    if not origin:
        return False
    allow_set = {o.strip().rstrip("/") for o in allowed.split(",") if o.strip()}
    return origin.rstrip("/") in allow_set


def _request_token(authorization: Optional[str], cookie_token: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return cookie_token


async def require_bearer(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    cookie_token = request.cookies.get(COOKIE_NAME)
    token = _request_token(authorization, cookie_token)
    if not check_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token"
        )


async def ws_authorize(ws: WebSocket, query_token: Optional[str]) -> bool:
    """Validate a WebSocket upgrade: token (cookie or query) AND origin allowlist."""
    if not _check_origin_allowed(ws.headers.get("origin")):
        logger.warning("ws upgrade rejected: origin=%r", ws.headers.get("origin"))
        return False
    token = query_token or ws.cookies.get(COOKIE_NAME)
    return check_token(token)


def install_login_routes(app) -> None:
    """Register /api/login and /api/logout. Called from main.py."""

    @app.post("/api/login")
    async def login(request: Request, response: Response):
        ip = _client_ip(request)
        if _login_rate_limited(ip):
            logger.warning("login rate-limited: ip=%s", ip)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many failed attempts; wait a minute",
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        submitted = body.get("token") if isinstance(body, dict) else None
        if not check_token(submitted):
            _record_login_fail(ip)
            logger.info("login failed: ip=%s", ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
            )
        _record_login_success(ip)
        secure = _is_secure_request(request)
        _set_session_cookie(response, submitted, secure=secure)
        logger.info("login ok: ip=%s secure=%s", ip, secure)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}
