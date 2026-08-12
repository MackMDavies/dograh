"""Supabase writes for the dialer call log.

dialer_calls lives entirely in Supabase (see
docs/superpowers/specs/2026-08-11-dialer-call-log-recording-design.md in
the sysevo repo) - same reasoning as dialer_number_assignment.py: no local
copy of this data in Dograh, so every write is a server-to-server Supabase
REST call using the service-role key.

Every function here is fail-soft: it must never raise. These run inside
Twilio webhook handlers AFTER Twilio already has what it needs to keep the
call running - a write failure here means a dialer_calls row goes
stale/unfilled, never a broken call.
"""

from datetime import UTC, datetime

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

_DIALER_CALLS_URL_SUFFIX = "/rest/v1/dialer_calls"


def _headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def create_dialer_call(
    *,
    parent_call_sid: str,
    rep_user_id: str,
    entry_id: str | None,
    from_number: str,
    to_number: str,
) -> None:
    """Create the initial dialer_calls row synchronously, before voice-connect
    returns TwiML - see the module docstring on why this can't wait for an
    async callback."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot create dialer_calls row")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                json={
                    "parent_call_sid": parent_call_sid,
                    "rep_user_id": rep_user_id,
                    "entry_id": entry_id,
                    "from_number": from_number,
                    "to_number": to_number,
                    "status": "initiated",
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to create dialer_calls row for {parent_call_sid}: {exc}")


async def update_dialer_call_status(
    *,
    parent_call_sid: str,
    child_call_sid: str | None,
    status: str,
    duration_seconds: int | None,
) -> None:
    """Update status/duration from the <Number>'s statusCallback."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls status")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}"},
                json={
                    "child_call_sid": child_call_sid,
                    "status": status,
                    "duration_seconds": duration_seconds,
                    "ended_at": datetime.now(UTC).isoformat() if status == "completed" else None,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls status for {parent_call_sid}: {exc}")


async def update_dialer_call_conference_sid(*, parent_call_sid: str, conference_sid: str) -> None:
    """Populates conference_sid once the Conference actually starts (the
    rep has joined) - this is later used to correlate the Conference's own
    recording callback, which identifies via ConferenceSid rather than any
    participant's CallSid."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls conference_sid")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}"},
                json={"conference_sid": conference_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls conference_sid for {parent_call_sid}: {exc}")


async def update_dialer_call_child_sid(*, parent_call_sid: str, child_call_sid: str) -> None:
    """Record the lead leg's SID as soon as the outbound dial returns.

    update_dialer_call_status also writes this column, but only when the
    lead's own status callback arrives - which the conference-end orphan
    cleanup can outrace, leaving it with no SID to end the leg by. This
    patches child_call_sid alone, deliberately NOT status, so it can never
    walk the row's status backwards past a callback that already landed.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls child_call_sid")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}"},
                json={"child_call_sid": child_call_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls child_call_sid for {parent_call_sid}: {exc}")


async def get_dialer_call_child_leg(*, parent_call_sid: str) -> dict | None:
    """Read back the lead leg's SID and last-known status.

    The only read in this module. dialer-conference-events uses it to decide
    whether the lead's outbound leg was left orphaned (still ringing, never
    a conference participant) when the conference ended.

    Returns None - never raises, never partially reports - when the row is
    missing or unreadable for any reason, so callers treat "don't know" and
    "nothing there" identically.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot read dialer_calls row")
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={
                    "select": "child_call_sid,status",
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    "limit": "1",
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to read dialer_calls row for {parent_call_sid}: {exc}")
        return None
    # A 200 from a PostgREST table GET is always a JSON array of objects
    # (error bodies are objects, but raise_for_status has already rejected
    # those, and we don't send the vnd.pgrst.object+json Accept header). The
    # shape check is for what sits BETWEEN us and PostgREST - a proxy or
    # captive portal answering 200 with a body that isn't ours at all - since
    # "never raises" has to hold for that too. Both halves earn their keep:
    # a non-list body is caught by the first, and a list of NON-objects
    # (["error"], [123], [["nested"]]) only by the second. Returning one of
    # those would break this function's dict|None contract and blow up in the
    # caller's .get() instead, outside any of this module's error handling.
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


async def update_dialer_call_recording(*, parent_call_sid: str, recording_sid: str) -> None:
    """Update recording_sid from the <Dial>'s recordingStatusCallback."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls recording")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}"},
                json={"recording_sid": recording_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls recording for {parent_call_sid}: {exc}")


async def update_dialer_call_recording_by_conference_sid(
    *, conference_sid: str, recording_sid: str
) -> None:
    """Update recording_sid from a Conference's recordingStatusCallback -
    correlates via conference_sid (populated by dialer-conference-events),
    not parent_call_sid, since a Conference recording's own callback
    identifies the conference via ConferenceSid, not any participant's
    CallSid - a genuinely different correlation key from the old
    <Dial record> callback shape."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls recording")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"conference_sid": f"eq.{conference_sid}"},
                json={"recording_sid": recording_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls recording for conference {conference_sid}: {exc}")
