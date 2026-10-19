"""Server-side SignalWire Calling API helpers for the sales dialer.

This module is deliberately separate from the browser subscriber flow. Parallel
dialing needs the server to originate and track each prospect leg independently;
the browser's single resource-address call cannot provide per-leg control.

SignalWire API reference: POST /api/calling/calls with command="dial".
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from api.services.telephony.dialer.signalwire_dialer import _space_host


class SignalWireCallError(RuntimeError):
    """SignalWire rejected or failed a server-originated dial request."""


def _calling_api_credentials() -> tuple[str, str, str]:
    host = _space_host()
    project = (os.environ.get("SIGNALWIRE_PROJECT_ID") or "").strip()
    token = (os.environ.get("SIGNALWIRE_API_TOKEN") or "").strip()
    if not (host and project and token):
        raise SignalWireCallError("SignalWire Calling API credentials are not configured")
    return f"https://{host}/api/calling/calls", project, token


async def originate_call(
    *,
    from_number: str,
    to_number: str,
    swml_url: str,
    status_url: str,
    status_events: tuple[str, ...] = ("created", "ringing", "answered", "ended"),
) -> dict[str, Any]:
    """Create one PSTN leg and return SignalWire's call object.

    A call is considered created only when SignalWire returns a non-empty call
    `id`; a successful HTTP status with an unexpected body is an error because
    the caller would otherwise have no handle for cleanup or reconciliation.
    """
    if not from_number.startswith("+") or not to_number.startswith("+"):
        raise SignalWireCallError("SignalWire PSTN endpoints must use E.164 numbers")
    if not swml_url.startswith("https://") or not status_url.startswith("https://"):
        raise SignalWireCallError("SignalWire call and status handlers must use HTTPS")
    allowed_events = {"created", "ringing", "answered", "ended"}
    if not status_events or any(event not in allowed_events for event in status_events):
        raise SignalWireCallError("Unsupported SignalWire call status event")

    endpoint, project, token = _calling_api_credentials()
    payload = {
        "command": "dial",
        "params": {
            "from": from_number,
            "to": to_number,
            "url": swml_url,
            "status_url": status_url,
            "status_events": list(status_events),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(endpoint, json=payload, auth=(project, token))
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPStatusError as exc:
        # The body may contain account-specific details; return a bounded error
        # to logs/callers without echoing credentials or the full response.
        raise SignalWireCallError(
            f"SignalWire rejected outbound call (HTTP {exc.response.status_code})"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SignalWireCallError("SignalWire outbound call request failed") from exc

    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"].strip():
        raise SignalWireCallError("SignalWire returned no call id for the outbound leg")
    return result


async def end_call(*, call_id: str, reason: str = "cancel") -> None:
    """End a losing or explicitly cancelled server-originated leg."""
    allowed_reasons = {"hangup", "cancel", "busy", "noAnswer", "decline", "error"}
    if not call_id or len(call_id) > 128:
        raise SignalWireCallError("SignalWire call id is invalid")
    if reason not in allowed_reasons:
        raise SignalWireCallError("Unsupported SignalWire hangup reason")
    endpoint, project, token = _calling_api_credentials()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                endpoint,
                json={"command": "calling.end", "id": call_id, "params": {"reason": reason}},
                auth=(project, token),
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SignalWireCallError(
            f"SignalWire could not end call (HTTP {exc.response.status_code})"
        ) from exc
    except httpx.HTTPError as exc:
        raise SignalWireCallError("SignalWire call termination request failed") from exc


async def update_call_swml(*, call_id: str, swml: dict[str, Any]) -> None:
    """Hand a live server-originated leg a new inline SWML document.

    Calling API ``update`` command (POST /api/calling/calls,
    ``{"command": "update", "params": {"id": ..., "swml": {...}}}``). Used to
    take a bridged mailbox leg out of the rep's conference and play the rep's
    recording on it, so the rep can leave while it plays.
    """
    if not call_id or len(call_id) > 128:
        raise SignalWireCallError("SignalWire call id is invalid")
    if not isinstance(swml, dict) or "sections" not in swml:
        raise SignalWireCallError("SignalWire update requires a SWML document")
    endpoint, project, token = _calling_api_credentials()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                endpoint,
                json={"command": "update", "params": {"id": call_id, "swml": swml}},
                auth=(project, token),
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SignalWireCallError(
            f"SignalWire could not update call (HTTP {exc.response.status_code})"
        ) from exc
    except httpx.HTTPError as exc:
        raise SignalWireCallError("SignalWire call update request failed") from exc
