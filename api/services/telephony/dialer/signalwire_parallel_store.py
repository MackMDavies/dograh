"""Durable state operations for SignalWire parallel dial batches.

These RPCs are deliberately fail-closed. A caller must never be originated
when the selected destinations were not reserved, and no leg may join the rep
unless the database's atomic winner claim succeeds.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

_RPC = "/rest/v1/rpc/"
logger = logging.getLogger(__name__)


class SignalWireParallelStoreError(RuntimeError):
    """Durable batch state could not be established or updated."""


def _headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise SignalWireParallelStoreError("Supabase service credentials are unavailable")
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _rpc(name: str, body: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}{_RPC}{name}", json=body, headers=_headers()
            )
            response.raise_for_status()
            return response.json()
    except SignalWireParallelStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - no call starts after an uncertain DB write
        raise SignalWireParallelStoreError(f"Could not persist SignalWire parallel state ({name})") from exc


async def start_session(
    *,
    session_id: str,
    rep_user_id: str,
    rep_call_id: str,
    conference_name: str,
    entry_ids: list[str],
    line_count: int,
) -> list[dict[str, Any]]:
    rows = await _rpc(
        "dialer_signalwire_parallel_start_v2",
        {
            "p_session_id": session_id,
            "p_rep_user_id": rep_user_id,
            "p_rep_call_id": rep_call_id,
            "p_conference_name": conference_name,
            "p_entry_ids": entry_ids,
            "p_line_count": line_count,
        },
    )
    if not isinstance(rows, list) or len(rows) != line_count or any(not isinstance(row, dict) for row in rows):
        raise SignalWireParallelStoreError("Parallel session did not reserve the exact requested lines")
    by_entry = {str(row.get("entry_id") or ""): row for row in rows}
    if set(by_entry) != set(entry_ids) or len({row.get("id") for row in rows}) != line_count:
        raise SignalWireParallelStoreError("Parallel reservation did not match the selected leads")
    # Bind the provider target to its own lead even if the transport reorders rows.
    return [by_entry[entry_id] for entry_id in entry_ids]


async def start_screened_single_session(
    *, session_id: str, rep_user_id: str, rep_call_id: str,
    conference_name: str, entry_id: str,
) -> list[dict[str, Any]]:
    """Reserve one queue entry for a SignalWire AMD-screened single call."""
    rows = await _rpc(
        "dialer_signalwire_screened_single_start_v2",
        {
            "p_session_id": session_id,
            "p_rep_user_id": rep_user_id,
            "p_rep_call_id": rep_call_id,
            "p_conference_name": conference_name,
            "p_entry_id": entry_id,
        },
    )
    if (
        not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
        or rows[0].get("entry_id") != entry_id
    ):
        raise SignalWireParallelStoreError("Screened single call did not reserve its lead")
    return rows


async def expire_stale_sessions(*, rep_user_id: str) -> int:
    """Close this rep's batches that no callback will ever close. Best-effort.

    Never raises: before migration 20270421090500 the RPC does not exist, and an
    unavailable cleanup must not stop a valid batch (the reservation RPC still
    enforces one active batch per rep).
    """
    try:
        result = await _rpc(
            "dialer_signalwire_parallel_expire_stale", {"p_rep_user_id": rep_user_id}
        )
    except SignalWireParallelStoreError:
        logger.warning("Could not expire stale SignalWire parallel sessions", exc_info=True)
        return 0
    if isinstance(result, int) and result > 0:
        logger.warning("Expired %s stale SignalWire parallel session(s) for a rep", result)
    return result if isinstance(result, int) else 0


async def register_provider_call(*, session_id: str, leg_id: str, provider_call_id: str) -> bool:
    changed = await _rpc(
        "dialer_signalwire_parallel_bind_call",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
        },
    )
    return changed is True


async def update_leg(
    *,
    session_id: str,
    leg_id: str,
    provider_call_id: str,
    status: str,
    amd_result: str | None = None,
    amd_ms: int | None = None,
) -> bool:
    changed = await _rpc(
        "dialer_signalwire_parallel_update_leg",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
            "p_status": status,
            "p_amd_result": amd_result,
            "p_amd_ms": amd_ms,
        },
    )
    return changed is True


async def claim_ringing_cancel(
    *, session_id: str, leg_id: str, provider_call_id: str,
) -> bool:
    """Claim sibling cleanup without racing an answered/screening transition."""
    result = await _rpc("dialer_signalwire_parallel_claim_ringing_cancel", {
        "p_session_id": session_id, "p_leg_id": leg_id,
        "p_provider_call_id": provider_call_id,
    })
    return result is True


async def claim_winner(
    *, session_id: str, leg_id: str, provider_call_id: str,
    amd_ms: int | None, amd_result: str = "human"
) -> bool:
    result = await _rpc(
        "dialer_signalwire_parallel_claim_winner",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
            "p_amd_ms": amd_ms,
            "p_amd_result": amd_result,
        },
    )
    return result is True


async def record_parallel_timing(
    *, session_id: str, leg_id: str | None, provider_call_id: str,
    event_kind: str, event_at: str,
) -> bool:
    """Persist provider timestamps for answer, rep join, and winner join events."""
    result = await _rpc(
        "dialer_signalwire_parallel_record_timing",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
            "p_event_kind": event_kind,
            "p_event_at": event_at,
        },
    )
    return result is True


async def claim_voicemail_drop(
    *, session_id: str, leg_id: str, provider_call_id: str, drop_id: str
) -> bool:
    result = await _rpc(
        "dialer_signalwire_parallel_claim_voicemail_drop",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
            "p_drop_id": drop_id,
        },
    )
    return result is True


async def complete_voicemail_drop(
    *, session_id: str, leg_id: str, provider_call_id: str
) -> bool:
    """Persist one completed drop, disposition and attempt in a single RPC."""
    result = await _rpc(
        "dialer_signalwire_parallel_complete_voicemail",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
        },
    )
    return result is True


async def record_outcome(
    *, session_id: str, leg_id: str, provider_call_id: str, outcome: str
) -> bool:
    """Write an automatic per-leg outcome and attempt exactly once."""
    result = await _rpc(
        "dialer_signalwire_parallel_record_outcome",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
            "p_outcome": outcome,
        },
    )
    return result is True


async def route_to_teammate(
    *, session_id: str, leg_id: str, provider_call_id: str
) -> dict[str, Any] | None:
    """Atomically reserve an available teammate and an isolated overflow room."""
    rows = await _rpc(
        "dialer_signalwire_parallel_route_to_teammate",
        {
            "p_session_id": session_id,
            "p_leg_id": leg_id,
            "p_provider_call_id": provider_call_id,
        },
    )
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


async def get_voicemail_policy(
    *, rep_user_id: str, entry_id: str, parent_call_sid: str
) -> dict[str, Any] | None:
    result = await _rpc(
        "dialer_account_voicemail_policy",
        {
            "p_rep_user_id": rep_user_id,
            "p_entry_id": entry_id,
            "p_parent_call_sid": parent_call_sid,
        },
    )
    return result if isinstance(result, dict) else None


async def finish_session(*, session_id: str, status: str) -> bool:
    result = await _rpc(
        "dialer_signalwire_parallel_finish",
        {"p_session_id": session_id, "p_status": status},
    )
    if result is True:
        # The shared server guard resolves either provider's rep call id and
        # independently verifies that every line has settled before adjusting.
        try:
            session = await get_session(session_id=session_id)
            if session and session.get("rep_call_id"):
                await _rpc(
                    "dialer_parallel_apply_abandon_guard",
                    {"p_parent_call_sid": session["rep_call_id"]},
                )
        except Exception:  # noqa: BLE001 - safeguard failure must not strand call cleanup
            logger.exception("Could not evaluate SignalWire parallel abandonment safeguard")
    return result is True


async def get_leg_context(*, session_id: str, leg_id: str) -> dict[str, Any] | None:
    """Load the server-owned session and leg before a provider decision is made."""
    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            session_response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_signalwire_parallel_sessions",
                params={"select": "id,rep_user_id,rep_call_id,conference_name,status,winner_leg_id,line_count", "id": f"eq.{session_id}", "limit": "1"},
                headers=headers,
            )
            session_response.raise_for_status()
            leg_response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_signalwire_parallel_legs",
                params={"select": "id,session_id,entry_id,provider_call_id,status", "id": f"eq.{leg_id}", "session_id": f"eq.{session_id}", "limit": "1"},
                headers=headers,
            )
            leg_response.raise_for_status()
            sessions, legs = session_response.json(), leg_response.json()
    except Exception as exc:  # noqa: BLE001 - callers must fail closed
        raise SignalWireParallelStoreError("Could not load SignalWire parallel leg state") from exc
    if not isinstance(sessions, list) or len(sessions) != 1 or not isinstance(legs, list) or len(legs) != 1:
        return None
    return {"session": sessions[0], "leg": legs[0]}


async def get_session(*, session_id: str) -> dict[str, Any] | None:
    """Load the durable batch identity used to authenticate rep callbacks."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_signalwire_parallel_sessions",
                params={
                    "select": "id,rep_user_id,rep_call_id,conference_name,status,winner_leg_id",
                    "id": f"eq.{session_id}", "limit": "1",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001
        raise SignalWireParallelStoreError("Could not load SignalWire parallel session") from exc
    return rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None


async def get_session_legs(*, session_id: str) -> list[dict[str, Any]]:
    """Return the durable line states used to decide whether a batch is over."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_signalwire_parallel_legs",
                params={"select": "id,status,provider_call_id", "session_id": f"eq.{session_id}"},
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - callers must fail closed
        raise SignalWireParallelStoreError("Could not load SignalWire batch line states") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SignalWireParallelStoreError("SignalWire batch line state response was invalid")
    return rows
