"""Service-role operations for a parallel dial session.

The provider owns child legs; Postgres owns which entry wins. All helpers fail
closed so a missing migration or unavailable ledger can never silently turn a
multi-lead request into an untracked parallel call.
"""

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


async def start_parallel_session(
    *, parent_call_sid: str, rep_user_id: str, entry_ids: list[str], line_count: int
) -> bool:
    """Validate visibility and persist the session before placing any lead leg."""
    if not _ready():
        logger.warning("Cannot start parallel dial session without Supabase service credentials")
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_start_parallel_session",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_rep_user_id": rep_user_id,
                    "p_entry_ids": entry_ids,
                    "p_line_count": line_count,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - no call legs without a durable session
        logger.error(f"Could not create parallel dial session for {parent_call_sid}: {exc}")
        return False


async def candidate_phones(entry_ids: list[str]) -> dict[str, str]:
    """Read requested destination numbers for a compliance preflight.

    Session creation still revalidates ownership and availability atomically;
    this read only lets the route check every destination before reserving a
    batch or asking Twilio to originate any child leg.
    """
    if not _ready() or not entry_ids:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/cold_call_entries",
                params={"select": "id,phone", "id": f"in.({','.join(entry_ids)})"},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list):
            return {}
        phones = {
            row["id"]: row["phone"]
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and isinstance(row.get("phone"), str)
        }
        return phones if len(phones) == len(entry_ids) else {}
    except Exception as exc:  # noqa: BLE001 - no targets on uncertain compliance input
        logger.error(f"Could not load parallel candidates for calling-hours preflight: {exc}")
        return {}


async def register_parallel_leg(
    *, leg_id: str, child_call_sid: str
) -> bool:
    if not _ready():
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_register_leg",
                json={"p_leg_id": leg_id, "p_child_call_sid": child_call_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - caller must stop a leg it failed to ledger
        logger.error(f"Could not register parallel child leg {child_call_sid}: {exc}")
        return False


async def assign_parallel_caller_id(
    *, parent_call_sid: str, leg_id: str, rep_user_id: str, caller_id: str
) -> bool:
    """Persist the server-selected caller ID before the child PSTN call starts."""
    if not _ready():
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_assign_caller_id",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_leg_id": leg_id,
                    "p_rep_user_id": rep_user_id,
                    "p_caller_id": caller_id,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - never originate an untracked caller ID
        logger.error(f"Could not persist caller ID for parallel leg {leg_id}: {exc}")
        return False


async def parallel_targets(*, parent_call_sid: str, rep_user_id: str) -> list[dict]:
    """Fetch the exact entries already validated into this live session."""
    if not _ready():
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_targets",
                json={"p_parent_call_sid": parent_call_sid, "p_rep_user_id": rep_user_id},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return []
        return [
            row for row in rows
            if isinstance(row.get("leg_id"), str)
            and isinstance(row.get("entry_id"), str)
            and isinstance(row.get("phone"), str)
            and isinstance(row.get("business"), str)
        ]
    except Exception as exc:  # noqa: BLE001 - invalid session data fails closed
        logger.error(f"Could not load parallel targets for {parent_call_sid}: {exc}")
        return []


async def claim_parallel_winner(
    *, parent_call_sid: str, entry_id: str, child_call_sid: str, leg_id: str
) -> bool:
    """Atomically claim the first human leg; exactly one concurrent caller wins."""
    if not _ready():
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_claim_winner",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_entry_id": entry_id,
                    "p_child_call_sid": child_call_sid,
                    "p_leg_id": leg_id,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - ambiguous winner means nobody is bridged
        logger.error(f"Could not claim parallel winner for {parent_call_sid}: {exc}")
        return False


async def route_parallel_to_teammate(
    *, parent_call_sid: str, child_call_sid: str, conference_name: str,
    entry_id: str, leg_id: str,
) -> dict | None:
    """Atomically reserve one reachable teammate and create their ring invite."""
    if not _ready():
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_route_to_teammate",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_child_call_sid": child_call_sid,
                    "p_conference_name": conference_name,
                    "p_entry_id": entry_id,
                    "p_leg_id": leg_id,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            row = rows[0]
            if isinstance(row.get("inbound_call_id"), str) and isinstance(row.get("rep_user_id"), str):
                return row
        return None
    except Exception as exc:  # noqa: BLE001 - no bridge without a durable reservation
        logger.error(f"Could not route second pickup {child_call_sid}: {exc}")
        return None


async def update_parallel_leg(
    *, child_call_sid: str | None, status: str, answered_by: str | None = None, leg_id: str | None = None
) -> bool:
    if not _ready():
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_update_leg",
                json={
                    "p_child_call_sid": child_call_sid,
                    "p_status": status,
                    "p_answered_by": answered_by,
                    "p_leg_id": leg_id,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - callback should fail soft but log state loss
        logger.error(f"Could not update parallel child leg {child_call_sid}: {exc}")
        return False


async def record_parallel_timing(
    *, parent_call_sid: str, child_call_sid: str, event_kind: str,
    event_at: str, leg_id: str | None = None,
) -> bool:
    """Persist provider event time for answer-to-rep connection measurements."""
    if not _ready():
        return False
    if event_kind not in {"answer", "rep_join", "lead_join"}:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_record_timing",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_child_call_sid": child_call_sid,
                    "p_event_kind": event_kind,
                    "p_event_at": event_at,
                    "p_leg_id": leg_id,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            recorded = response.json() is True
        if recorded and event_kind != "answer":
            await apply_parallel_abandon_guard(parent_call_sid=parent_call_sid)
        return recorded
    except Exception as exc:  # noqa: BLE001 - measurement loss must be visible
        logger.error(f"Could not record parallel {event_kind} timing for {parent_call_sid}: {exc}")
        return False


async def apply_parallel_abandon_guard(*, parent_call_sid: str) -> dict | None:
    """Evaluate the measured 30-day guard after a provider state transition."""
    if not _ready():
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_apply_abandon_guard",
                json={"p_parent_call_sid": parent_call_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            result = response.json()
        return result if isinstance(result, dict) else None
    except Exception as exc:  # noqa: BLE001 - guard failure is logged for operations
        logger.error(f"Could not evaluate parallel abandon guard for {parent_call_sid}: {exc}")
        return None


async def parallel_legs(*, parent_call_sid: str) -> list[dict]:
    if not _ready():
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_parallel_legs",
                params={
                    "select": "leg_id,parent_call_sid,child_call_sid,entry_id,status,answered_by,auto_voicemail_sent,caller_id",
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    "order": "started_at.asc",
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        return rows if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else []
    except Exception as exc:  # noqa: BLE001 - cleanup can only target known rows
        logger.error(f"Could not list parallel legs for {parent_call_sid}: {exc}")
        return []


async def parallel_session(*, parent_call_sid: str) -> dict | None:
    if not _ready():
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_parallel_sessions",
                params={
                    "select": "parent_call_sid,status,winning_entry_id,winning_child_call_sid",
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    "limit": "1",
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    except Exception as exc:  # noqa: BLE001 - session uncertainty must not invent a winner
        logger.error(f"Could not load parallel session {parent_call_sid}: {exc}")
        return None


async def finish_parallel_session(*, parent_call_sid: str, status: str) -> bool:
    if status not in {"completed", "failed", "cancelled"} or not _ready():
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_finish_session",
                json={"p_parent_call_sid": parent_call_sid, "p_status": status},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            finished = response.json() is True
        if finished:
            await apply_parallel_abandon_guard(parent_call_sid=parent_call_sid)
        return finished
    except Exception as exc:  # noqa: BLE001 - no late leg should be accepted on uncertain cleanup
        logger.error(f"Could not finish parallel session {parent_call_sid}: {exc}")
        return False


async def available_teammates(*, rep_user_id: str) -> list[dict]:
    """Return reachable overflow candidates from only this rep's dialer account.

    Account resolution, self-exclusion and candidate filtering remain in one
    database function so provider code cannot widen scope or race a tenant
    change by assembling a global roster itself.
    """
    if not _ready() or not rep_user_id:
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_available_teammates",
                json={"p_rep_user_id": rep_user_id},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list):
            return []
        return [
            row for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("user_id"), str)
            and isinstance(row.get("full_name"), str)
            and row.get("reach") in {"live", "push"}
        ]
    except Exception as exc:  # noqa: BLE001 - no global fallback for overflow routing
        logger.error(f"Could not load same-account overflow candidates for {rep_user_id}: {exc}")
        return []
