"""Org Langfuse configuration: storage format, credential check, and send options.

Stored in ``organization_configurations`` under ``LANGFUSE_CREDENTIALS``::

    {"host": "...", "public_key": "pk-lf-...", "secret_key_enc": "<fernet>",
     "options": {"traces": true, "spans": true, "tools": true,
                 "transcripts": false, "redact": true}}

The secret is encrypted at rest with ``api.services.crypto`` (same scheme as the
platform telephony secrets). Rows written before this change hold a plaintext
``secret_key`` and no ``options``; they are read unchanged (all options on except
redaction, which matches what was sent before) and re-encrypted on the next save.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx
from loguru import logger

from api.services.crypto import decrypt_secret, encrypt_secret

OPTION_KEYS = ("traces", "spans", "tools", "transcripts", "redact")
# What new configurations default to (the Developers page's initial checklist).
DEFAULT_OPTIONS: Dict[str, bool] = {
    "traces": True,
    "spans": True,
    "tools": True,
    "transcripts": False,
    "redact": True,
}
# Rows saved before options existed exported everything, unredacted.
LEGACY_OPTIONS: Dict[str, bool] = {
    "traces": True,
    "spans": True,
    "tools": True,
    "transcripts": True,
    "redact": False,
}


def normalize_options(raw: Any, fallback: Dict[str, bool] = DEFAULT_OPTIONS) -> Dict[str, bool]:
    out = dict(fallback)
    if isinstance(raw, dict):
        for k in OPTION_KEYS:
            if k in raw:
                out[k] = bool(raw[k])
    return out


def decode_langfuse_config(value: Optional[dict]) -> Optional[Dict[str, Any]]:
    """Stored value → {host, public_key, secret_key, options} (secret decrypted)."""
    if not value:
        return None
    secret = value.get("secret_key") or ""
    enc = value.get("secret_key_enc")
    if enc:
        try:
            secret = decrypt_secret(enc)
        except Exception:
            logger.warning("Langfuse secret could not be decrypted (OSS_JWT_SECRET rotated?)")
            secret = ""
    return {
        "host": (value.get("host") or "").rstrip("/"),
        "public_key": value.get("public_key") or "",
        "secret_key": secret,
        "options": normalize_options(
            value.get("options"), DEFAULT_OPTIONS if "options" in value else LEGACY_OPTIONS
        ),
    }


def encode_langfuse_config(host: str, public_key: str, secret_key: str, options: Dict[str, bool]) -> dict:
    return {
        "host": host.rstrip("/"),
        "public_key": public_key,
        "secret_key_enc": encrypt_secret(secret_key),
        "options": normalize_options(options),
    }


def validate_host(host: str) -> Optional[str]:
    """None if ``host`` is an acceptable Langfuse base URL, else the reason.

    The server fetches this URL, so only public https hosts are allowed.
    """
    try:
        u = urlparse(host.strip())
    except Exception:
        return "Host must be a URL"
    if u.scheme != "https" or not u.hostname:
        return "Host must start with https://"
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return "Host could not be resolved"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return "Host must be a public address"
    return None


async def verify_langfuse(host: str, public_key: str, secret_key: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Check the keys against Langfuse's public API.

    Returns (ok, traces_today, error). ``traces_today`` counts traces in the project
    since 00:00 UTC (None when Langfuse doesn't report a total).
    """
    reason = validate_host(host)
    if reason:
        return False, None, reason
    base = host.rstrip("/")
    auth = (public_key, secret_key)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            r = await client.get(f"{base}/api/public/projects", auth=auth)
            if r.status_code in (401, 403):
                return False, None, "Langfuse rejected these keys"
            if r.status_code >= 400:
                return False, None, f"Langfuse returned {r.status_code}"
            since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            t = await client.get(
                f"{base}/api/public/traces",
                params={"fromTimestamp": since.isoformat().replace("+00:00", "Z"), "limit": 1},
                auth=auth,
            )
            total = None
            if t.status_code < 400:
                meta = (t.json() or {}).get("meta") or {}
                if isinstance(meta.get("totalItems"), int):
                    total = meta["totalItems"]
            return True, total, None
    except httpx.TimeoutException:
        return False, None, "Langfuse did not answer in 10 s"
    except Exception as e:
        logger.debug(f"Langfuse verify failed: {e}")
        return False, None, "Could not reach Langfuse"
