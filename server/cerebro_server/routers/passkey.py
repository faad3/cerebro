"""WebAuthn / passkey endpoints.

Flow:
  GET  /api/passkey/status                 → {enabled, has_credentials, rp_id}
  POST /api/passkey/register/begin         → {ticket, options}   (auth required)
  POST /api/passkey/register/complete      → {credential_id}     (auth required)
  POST /api/passkey/login/begin            → {ticket, options}   (no auth)
  POST /api/passkey/login/complete         → sets cookie, 200    (no auth)
  GET  /api/passkey/devices                → [{id,label,...}]    (auth required)
  DELETE /api/passkey/devices/{cid}        → 204                 (auth required)
"""

import base64
import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from .. import audit, passkeys
from ..auth import (
    COOKIE_NAME,
    LOGIN_MAX_FAILS,
    LOGIN_WINDOW_SECONDS,
    _LOGIN_FAILS,
    _is_secure_request,
    _login_rate_limited,
    _record_login_fail,
    _record_login_success,
    _set_session_cookie,
    expected_token,
    require_bearer,
)

logger = logging.getLogger("cerebro.passkey")

router = APIRouter(prefix="/api/passkey", tags=["passkey"])


def _rp_id(request: Request) -> str:
    """Relying-party identifier — the hostname WebAuthn binds the credential to."""
    explicit = os.environ.get("CEREBRO_RP_ID", "").strip()
    if explicit:
        return explicit
    # Honor X-Forwarded-Host from a trusted proxy first.
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return host.split(":")[0] or "localhost"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _decode_client_response(payload: dict) -> dict:
    """fido2 v2's `RegistrationResponse.from_dict` / `AuthenticationResponse.from_dict`
    accept the raw browser JSON: base64url strings for byte fields, no manual
    decoding needed. We just normalize `id` so it equals `rawId` (fido2's
    `_parse_from_dict` compares the strings directly).
    """
    if not payload:
        raise ValueError("missing client response")
    out = dict(payload)
    if isinstance(out.get("rawId"), str):
        # Force id == rawId (some browsers / WebAuthn polyfills omit padding inconsistently).
        out["id"] = out["rawId"]
    return out


@router.get("/status")
async def status_route(request: Request):
    return {
        "enabled": passkeys.HAS_FIDO2,
        "has_credentials": passkeys.HAS_FIDO2 and passkeys.has_any(),
        "rp_id": _rp_id(request),
    }


@router.post("/register/begin", dependencies=[Depends(require_bearer)])
async def register_begin(request: Request):
    if not passkeys.HAS_FIDO2:
        raise HTTPException(503, "fido2 library not installed")
    body = await request.json() if request.headers.get("content-length") else {}
    label = (body or {}).get("label") or _default_label(request)
    ticket, options = passkeys.register_begin(_rp_id(request), label)
    return {"ticket": ticket, "options": options}


@router.post("/register/complete", dependencies=[Depends(require_bearer)])
async def register_complete(request: Request):
    if not passkeys.HAS_FIDO2:
        raise HTTPException(503, "fido2 library not installed")
    body = await request.json()
    ticket = body.get("ticket")
    client_response = _decode_client_response(body.get("response") or {})
    try:
        cid = passkeys.register_complete(_rp_id(request), ticket, client_response)
    except Exception as exc:
        logger.warning("passkey register_complete failed: %s", exc)
        raise HTTPException(400, f"registration failed: {exc}")
    audit.log("passkey.register", request, credential_id=cid)
    return {"credential_id": cid}


@router.post("/login/begin")
async def login_begin(request: Request):
    if not passkeys.HAS_FIDO2 or not passkeys.has_any():
        raise HTTPException(404, "no passkeys registered")
    ticket, options = passkeys.login_begin(_rp_id(request))
    return {"ticket": ticket, "options": options}


@router.post("/login/complete")
async def login_complete(request: Request, response: Response):
    from ..auth import _client_ip  # reuse helper
    ip = _client_ip(request)
    if _login_rate_limited(ip):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    if not passkeys.HAS_FIDO2:
        raise HTTPException(503, "fido2 library not installed")
    body = await request.json()
    ticket = body.get("ticket")
    client_response = _decode_client_response(body.get("response") or {})
    try:
        cid = passkeys.login_complete(_rp_id(request), ticket, client_response)
    except Exception as exc:
        _record_login_fail(ip)
        logger.info("passkey login failed: ip=%s err=%s", ip, exc)
        raise HTTPException(401, "passkey authentication failed")
    _record_login_success(ip)
    # Mint the session cookie with the master token value (existing model).
    # When we layer signed sessions on top, this is the single place to swap.
    token = expected_token()
    if not token:
        raise HTTPException(500, "server has no master token configured")
    _set_session_cookie(response, token, secure=_is_secure_request(request))
    audit.log("passkey.login", request, credential_id=cid)
    return {"ok": True, "credential_id": cid}


@router.get("/devices", dependencies=[Depends(require_bearer)])
async def list_devices():
    return passkeys.list_devices()


@router.delete("/devices/{cid}", dependencies=[Depends(require_bearer)])
async def revoke_device(cid: str, request: Request):
    ok = passkeys.remove(cid)
    if not ok:
        raise HTTPException(404, "credential not found")
    audit.log("passkey.revoke", request, credential_id=cid)
    return {"ok": True}


def _default_label(request: Request) -> str:
    """A friendly auto-label derived from User-Agent for the device list."""
    ua = (request.headers.get("user-agent") or "").lower()
    if "mac os" in ua or "macintosh" in ua:
        return "mac"
    if "iphone" in ua: return "iphone"
    if "android" in ua: return "android"
    if "windows" in ua: return "windows"
    if "linux" in ua: return "linux"
    return "device"
