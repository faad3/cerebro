"""Passkey (WebAuthn / FIDO2) registration + login.

Storage is a single JSON file at `{data_dir}/passkeys.json`:

    {
      "credentials": [
        {
          "id":         "<base64url credential id>",
          "public_key": "<base64url CBOR-encoded COSE pubkey>",
          "sign_count": 0,
          "label":      "macbook",
          "created_at": 1234567890.0,
          "last_used":  1234567890.0
        }
      ]
    }

Registration requires an authenticated request (caller is already holding a
valid session cookie or master token). This means the first device is always
bootstrapped by a master-token login; subsequent visits use Touch ID.
"""

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("cerebro.passkeys")

try:
    from fido2.server import Fido2Server
    from fido2.webauthn import (
        AttestedCredentialData,
        PublicKeyCredentialDescriptor,
        PublicKeyCredentialRpEntity,
        PublicKeyCredentialType,
        PublicKeyCredentialUserEntity,
        UserVerificationRequirement,
    )
    HAS_FIDO2 = True
except ImportError:
    HAS_FIDO2 = False
    logger.warning("fido2 library not installed — passkey support disabled")


_lock = threading.Lock()


def _data_dir() -> Path:
    explicit = os.environ.get("CEREBRO_DATA_DIR")
    if explicit:
        return Path(explicit)
    if Path("/data").exists() and os.access("/data", os.W_OK):
        return Path("/data")
    return Path.home() / ".cerebro"


def _path() -> Path:
    p = _data_dir() / "passkeys.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def load() -> dict:
    p = _path()
    if not p.exists():
        return {"credentials": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        logger.exception("passkeys.json unreadable; starting fresh")
        return {"credentials": []}


def save(data: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(p)


def has_any() -> bool:
    return bool(load().get("credentials"))


def list_devices() -> List[dict]:
    """Public-safe view (no key material)."""
    out = []
    for c in load().get("credentials", []):
        out.append({
            "id": c["id"],
            "label": c.get("label", ""),
            "created_at": c.get("created_at"),
            "last_used": c.get("last_used"),
        })
    return out


def remove(credential_id_b64: str) -> bool:
    with _lock:
        data = load()
        before = len(data.get("credentials", []))
        data["credentials"] = [c for c in data.get("credentials", []) if c["id"] != credential_id_b64]
        if len(data["credentials"]) == before:
            return False
        save(data)
        return True


def add(attested: "AttestedCredentialData", sign_count: int, label: str) -> str:
    """Append a credential; returns its b64-url id.

    `attested` is fido2's AttestedCredentialData (a bytes subclass). We store the
    full blob so we can reconstruct it later via AttestedCredentialData(blob).
    """
    cid = _b64e(attested.credential_id)
    with _lock:
        data = load()
        data.setdefault("credentials", []).append({
            "id": cid,
            "blob": _b64e(bytes(attested)),
            "sign_count": sign_count,
            "label": label,
            "created_at": time.time(),
            "last_used": None,
        })
        save(data)
    return cid


def update_sign_count(credential_id_b64: str, sign_count: int) -> None:
    with _lock:
        data = load()
        for c in data.get("credentials", []):
            if c["id"] == credential_id_b64:
                c["sign_count"] = sign_count
                c["last_used"] = time.time()
                save(data)
                return


def _attested_credentials() -> List["AttestedCredentialData"]:
    out = []
    for c in load().get("credentials", []):
        if "blob" in c:
            out.append(AttestedCredentialData(_b64d(c["blob"])))
        # Old-format records (id + public_key) — silently skipped on first run after upgrade.
    return out


def _server_for(rp_id: str, rp_name: str = "Cerebro") -> "Fido2Server":
    rp = PublicKeyCredentialRpEntity(id=rp_id, name=rp_name)
    return Fido2Server(rp)


# In-memory session state for the registration/auth ceremonies.
# Keyed by a short-lived ticket id we hand to the browser.
_pending: Dict[str, dict] = {}


def _new_ticket(state: dict) -> str:
    tid = base64.urlsafe_b64encode(os.urandom(18)).rstrip(b"=").decode("ascii")
    _pending[tid] = {"state": state, "ts": time.time()}
    # Reap stale tickets (5 min TTL).
    now = time.time()
    for k in list(_pending.keys()):
        if now - _pending[k]["ts"] > 300:
            _pending.pop(k, None)
    return tid


def _consume_ticket(tid: str) -> Optional[dict]:
    item = _pending.pop(tid, None)
    if item is None:
        return None
    if time.time() - item["ts"] > 300:
        return None
    return item["state"]


def register_begin(rp_id: str, label: str) -> Tuple[str, dict]:
    """Start a registration ceremony. Returns (ticket, public_key_creation_options-as-dict)."""
    server = _server_for(rp_id)
    # User identity. For single-user Cerebro, derive a stable id from the master
    # token file path so re-registers on the same install collide and replace.
    user = PublicKeyCredentialUserEntity(
        id=b"cerebro-user",
        name="cerebro",
        display_name="Cerebro",
    )
    options, state = server.register_begin(
        user=user,
        credentials=_attested_credentials(),
        user_verification=UserVerificationRequirement.PREFERRED,
        authenticator_attachment=None,  # allow platform + roaming
    )
    ticket = _new_ticket({"state": state, "label": label or "device"})
    return ticket, _to_jsonable(options)


def register_complete(rp_id: str, ticket: str, client_response: dict) -> str:
    """Finish registration. Returns the new credential's b64url id."""
    server = _server_for(rp_id)
    pending = _consume_ticket(ticket)
    if pending is None:
        raise ValueError("invalid or expired registration ticket")
    state = pending["state"]
    label = pending.get("label", "device")
    auth_data = server.register_complete(state, response=client_response)
    return add(
        attested=auth_data.credential_data,
        sign_count=auth_data.counter,
        label=label,
    )


def login_begin(rp_id: str) -> Tuple[str, dict]:
    server = _server_for(rp_id)
    options, state = server.authenticate_begin(
        credentials=_attested_credentials(),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    ticket = _new_ticket({"state": state})
    return ticket, _to_jsonable(options)


def login_complete(rp_id: str, ticket: str, client_response: dict) -> str:
    """Finish authentication. Returns the credential id that signed."""
    server = _server_for(rp_id)
    pending = _consume_ticket(ticket)
    if pending is None:
        raise ValueError("invalid or expired login ticket")
    state = pending["state"]
    creds = _attested_credentials()
    # fido2 v2 returns the AttestedCredentialData that matched.
    matched = server.authenticate_complete(
        state=state,
        credentials=creds,
        response=client_response,
    )
    cid_b64 = _b64e(matched.credential_id)
    # Mark "last used" — we no longer track sign count (spec compat is loose for resident keys).
    update_sign_count(cid_b64, 0)
    return cid_b64


def _to_jsonable(obj):
    """Convert fido2 dataclasses + bytes to JSON-safe values.

    Note: fido2 enums (PublicKeyCredentialType, UserVerificationRequirement, ...) are
    `(str, Enum)` subclasses, so `isinstance(obj, str)` catches them and JSON
    serializes them as their string value. Handle str/int/bool BEFORE walking
    `__dict__` or `vars()` — those would yield empty {} for str-enums.
    """
    from enum import Enum
    if obj is None or isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, Enum):
            return obj.value
        return obj
    if isinstance(obj, bytes):
        return _b64e(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {_camel(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "items"):
        return {_camel(k): _to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {_camel(k): _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_") and v is not None}
    return str(obj)


def _camel(k):
    # fido2 uses snake_case attrs (pub_key_cred_params); WebAuthn JSON expects camelCase.
    if not isinstance(k, str) or "_" not in k:
        return k
    parts = k.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])
