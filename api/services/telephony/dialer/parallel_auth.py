"""Short-lived proof that a SignalWire browser call was launched by a rep.

SignalWire uses one billable shared subscriber for the Dialer, so its caller
identity cannot safely authorize a rep or manager. The authenticated
`/voice-token` route issues this separate, short-lived token; webhook user
variables alone are never trusted for parallel session ownership.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid

TOKEN_TTL_SECONDS = 300


def _secret() -> bytes:
    value = (os.environ.get("SIGNALWIRE_PARALLEL_AUTH_SECRET") or os.environ.get("INTERNAL_API_SECRET") or "").strip()
    return value.encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issue_parallel_auth_token(supabase_user_id: str | None, *, now: int | None = None) -> str:
    if not supabase_user_id:
        return ""
    try:
        subject = str(uuid.UUID(supabase_user_id))
    except (ValueError, TypeError, AttributeError):
        return ""
    secret = _secret()
    if not secret:
        return ""
    payload = _b64url(
        json.dumps(
            {"sub": subject, "exp": (int(time.time()) if now is None else now) + TOKEN_TTL_SECONDS},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url(signature)}"


def verify_parallel_auth_token(token: str, *, now: int | None = None) -> str | None:
    secret = _secret()
    if not secret or not isinstance(token, str) or token.count(".") != 1:
        return None
    payload_part, signature_part = token.split(".", 1)
    expected = _b64url(hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature_part, expected):
        return None
    try:
        padded = payload_part + "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        subject = str(uuid.UUID(payload["sub"]))
        expires_at = int(payload["exp"])
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        return None
    current_time = int(time.time()) if now is None else now
    if expires_at <= current_time or expires_at > current_time + TOKEN_TTL_SECONDS:
        return None
    return subject
