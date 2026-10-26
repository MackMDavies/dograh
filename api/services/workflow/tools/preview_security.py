"""Boundary checks for the new tool-editor preview endpoint (no live-call changes)."""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from api.utils.url_security import _is_blocked_saas_service_ip, _resolve_hostname_ips


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The tool needs an https:// address without embedded credentials")
    return parsed.scheme, parsed.hostname.lower().rstrip("."), parsed.port or 443


def validate_preview_target(saved_url: str, target_url: str, *, has_credentials: bool) -> None:
    """Resolve public destinations and keep saved headers/credentials on their saved origin."""
    target_origin = _origin(target_url)
    if has_credentials and target_origin != _origin(saved_url):
        raise ValueError("Save the tool's new endpoint before testing it with its saved credential or headers")
    addresses = _resolve_hostname_ips(target_origin[1], target_origin[2])
    if not addresses or any(_is_blocked_saas_service_ip(ip) for ip in addresses):
        raise ValueError("Tool previews must resolve to a public IP address")


def mask_preview_headers(headers: dict[str, str], credential_header_names: Iterable[str] = ()) -> dict[str, str]:
    secret_names = {name.lower() for name in credential_header_names}
    return {
        name: "••••" if name.lower() in secret_names or re.search(r"auth|key|token|secret|cookie", name, re.I) else value
        for name, value in headers.items()
    }
