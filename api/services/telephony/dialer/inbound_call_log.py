"""Writing inbound calls into Supabase.

Same contract as dialer_call_log: this module never raises. A caller is on the line
while it runs, and a Supabase blip must cost us the record, never the call.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

_INBOUND_CALLS_URL = "/rest/v1/inbound_calls"
_RPC_URL = "/rest/v1/rpc"


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def resolve_inbound_plan(to_number: str) -> list[dict[str, Any]]:
    """Who to ring, how each is reachable, and the mobile to fall back to.

    Richer than resolve_inbound_targets because the routing decision needs all three: a
    list of ids alone cannot tell us whether anybody can actually pick up in a browser,
    which is the difference between holding the caller and putting them through.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot resolve inbound plan")
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_RPC_URL}/inbound_route_plan",
                json={"p_to_number": to_number},
                headers=_headers(),
                timeout=3.0,
            )
            response.raise_for_status()
            data = response.json()
            return list(data) if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 - never break call setup
        logger.error(f"Failed to resolve inbound plan for {to_number}: {exc}")
        return []


async def resolve_inbound_targets(to_number: str) -> list[str]:
    """Which reps should ring for a call to `to_number`.

    Delegates to inbound_route_targets so the rule -- assigned rep first, then everyone
    available -- lives in one place rather than being reimplemented here and drifting.

    An empty list is a real answer, not a failure: it means nobody can take the call, and
    the caller should go to the fallback rather than listen to hold music forever.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot resolve inbound targets")
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_RPC_URL}/inbound_route_targets",
                json={"p_to_number": to_number},
                headers=_headers(),
                timeout=3.0,
            )
            response.raise_for_status()
            data = response.json()
            return list(data) if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 - never break call setup
        logger.error(f"Failed to resolve inbound targets for {to_number}: {exc}")
        return []


async def is_claimed_inbound_conference(*, conference_name: str, rep_user_id: str) -> bool:
    """Fail closed unless this rep atomically claimed the conference invitation."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not conference_name or not rep_user_id:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}{_INBOUND_CALLS_URL}",
                params={
                    "select": "id",
                    "conference_name": f"eq.{conference_name}",
                    "status": "eq.answered",
                    "answered_by": f"eq.{rep_user_id}",
                    "limit": "1",
                },
                headers=_headers(),
                timeout=3.0,
            )
            response.raise_for_status()
            rows = response.json()
        return isinstance(rows, list) and bool(rows)
    except Exception as exc:  # noqa: BLE001 - no conference without an authoritative claim
        logger.error(f"Could not validate claimed inbound conference: {exc}")
        return False


async def _inbound_rpc(name: str, body: dict[str, Any]) -> Any:
    """Service-only conference bookkeeping; never grant a bridge on failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_RPC_URL}/{name}", json=body,
                headers=_headers(), timeout=3.0,
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001 - signed callbacks can be retried
        logger.error(f"Inbound conference bookkeeping failed ({name}): {exc}")
        return None


async def bind_inbound_rep_leg(
    *, conference_name: str, rep_user_id: str, provider: str, rep_call_id: str,
) -> bool:
    """Bind only the provider leg of the rep who claimed this invitation."""
    return await _inbound_rpc("dialer_bind_inbound_rep_leg", {
        "p_conference_name": conference_name, "p_rep_user_id": rep_user_id,
        "p_provider": provider, "p_rep_call_id": rep_call_id,
    }) is True


async def record_overflow_join(
    *, conference_name: str, provider: str, participant_call_id: str, event_at: str,
) -> bool | None:
    """Measure the actual overflow connection, independently of invite acceptance."""
    result = await _inbound_rpc("dialer_record_overflow_join", {
        "p_conference_name": conference_name, "p_provider": provider,
        "p_participant_call_id": participant_call_id, "p_event_at": event_at,
    })
    return result if isinstance(result, bool) else None


async def evaluate_overflow_guard(*, provider_call_id: str, provider: str) -> None:
    """Revisit the original batch when its separate teammate call settles."""
    await _inbound_rpc("dialer_evaluate_overflow_guard", {
        "p_provider_call_id": provider_call_id, "p_provider": provider,
    })


async def close_overflow_conference(
    *, conference_name: str, provider: str, participant_call_id: str,
) -> bool | None:
    """Release a teammate reservation only for a stored conference participant."""
    result = await _inbound_rpc("dialer_close_overflow_conference", {
        "p_conference_name": conference_name, "p_provider": provider,
        "p_participant_call_id": participant_call_id,
    })
    return result if isinstance(result, bool) else None


async def create_inbound_call(
    *,
    provider_call_id: str,
    from_number: str,
    to_number: str,
    conference_name: str,
    target_user_ids: list[str],
    caller_name: str | None = None,
) -> dict[str, Any] | None:
    """Record the ringing call. Returns the created row, or None if it could not be written.

    Written BEFORE the hold SWML is returned, not after: the row is what every rep's
    browser subscribes to, so a caller who is already on hold with no row is a caller
    nobody is being told about.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot create inbound_calls row")
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_INBOUND_CALLS_URL}",
                json={
                    "provider_call_id": provider_call_id,
                    "provider": "signalwire",
                    "from_number": from_number,
                    "to_number": to_number,
                    "conference_name": conference_name,
                    "target_user_ids": target_user_ids,
                    "caller_name": caller_name,
                    # No available reps is a missed call the moment it arrives. Marking it
                    # 'ringing' would leave a row nobody can see (RLS keys on
                    # target_user_ids) sitting in the list forever.
                    "status": "ringing" if target_user_ids else "missed",
                },
                headers=_headers(),
                timeout=3.0,
            )
            response.raise_for_status()
            rows = response.json()
            row = rows[0] if isinstance(rows, list) and rows else None
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to create inbound_calls row for {provider_call_id}: {exc}")
        return None

    # Tell their phones -- WITHOUT waiting for it.
    #
    # This used to be awaited, despite _notify_targets' own docstring calling itself
    # fire-and-forget. A caller is on the line and SignalWire is holding the call waiting
    # for a SWML document, and awaiting this put an HTTP hop to an edge function -- which
    # then calls OneSignal -- on the critical path before that document could be returned.
    # The call rang once and dropped, and the push landed just as it died, because the
    # push was the thing delaying the answer.
    #
    # create_task, so it runs after the response has gone out. A push that fails now costs
    # a notification. Awaiting it cost the call.
    if row and row.get("id") and target_user_ids:
        notify_inbound_call(str(row["id"]))
    return row


# Background tasks, held so they are not collected mid-flight.
#
# asyncio keeps only a weak reference to a running task. A task nobody holds can be
# garbage-collected before it finishes -- and the failure is silent: the push simply never
# arrives, with no error and nothing in the log. That is indistinguishable from the call
# dying first, which is exactly the confusion this code was already causing.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Run a coroutine after the response has gone out, and keep it alive until it ends."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop. Nothing to schedule onto, and a notification is never worth
        # raising into a live call path.
        coro.close()
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def notify_inbound_call(inbound_call_id: str) -> None:
    """Send a best-effort push after a durable ringing row has been written."""
    if inbound_call_id:
        _spawn(_notify_targets(inbound_call_id))


async def expire_unanswered_inbound_call(*, provider_call_id: str, provider: str = "twilio") -> bool:
    """Atomically expire a still-ringing invite; return true only for the winner.

    This lets a timed overflow offer be safely torn down without racing a rep
    who claims the call at the same time.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        now = datetime.now(UTC).isoformat()
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_INBOUND_CALLS_URL}",
                params={
                    "provider_call_id": f"eq.{provider_call_id}",
                    "provider": f"eq.{provider}",
                    "status": "eq.ringing",
                    "answered_by": "is.null",
                },
                json={"status": "missed", "ended_at": now, "updated_at": now},
                headers=_headers(),
                timeout=3.0,
            )
            response.raise_for_status()
            rows = response.json()
        return isinstance(rows, list) and bool(rows)
    except Exception as exc:  # noqa: BLE001 - a timeout is not permission to end the leg
        logger.error(f"Could not safely expire inbound offer {provider_call_id}: {exc}")
        return False


async def _notify_targets(inbound_call_id: str) -> None:
    """Push the ringing call to every target's devices.

    Fire-and-forget, and deliberately after the row exists: the row is what the browser
    subscribes to and what the notification links at, so a push that outran it would open
    an app showing nothing. A push failure must never cost the caller their call, so this
    swallows everything -- the overlay still rings for anyone who is watching.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/functions/v1/inbound-call-notify",
                json={"inbound_call_id": inbound_call_id},
                headers={
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Content-Type": "application/json",
                },
                timeout=8.0,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not push inbound call {inbound_call_id}: {exc}")


async def update_inbound_call(
    *,
    provider_call_id: str,
    patch: dict[str, Any],
    extra_filters: dict[str, str] | None = None,
) -> None:
    """Patch an inbound call by its provider id.

    `extra_filters` are PostgREST filters ANDed with the id, so a patch can be made
    conditional on the row's current state -- "only if it is still ringing" -- without
    a read-then-write race against the rep who is answering it at that moment.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update inbound_calls row")
        return
    try:
        params = {"provider_call_id": f"eq.{provider_call_id}"}
        params.update(extra_filters or {})
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_INBOUND_CALLS_URL}",
                params=params,
                json=patch,
                headers=_headers(),
                timeout=3.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to update inbound_calls row {provider_call_id}: {exc}")


async def close_inbound_call(*, provider_call_id: str) -> None:
    """End a call, marking it missed if it was still ringing when the caller gave up.

    Two conditional patches rather than one, because the two cases need different
    values and must not overwrite each other:

      * still 'ringing' -> nobody picked up, so it becomes 'missed'
      * already 'answered' -> keep that status, just stamp the end time

    Doing it as one unconditional patch is what made every unanswered call sit in the
    history reading "Ringing" forever, with the missed counter permanently at zero.
    """
    await update_inbound_call(
        provider_call_id=provider_call_id,
        patch={"status": "missed", "ended_at": "now()", "updated_at": "now()"},
        extra_filters={"status": "eq.ringing"},
    )
    await update_inbound_call(
        provider_call_id=provider_call_id,
        patch={"ended_at": "now()", "updated_at": "now()"},
        extra_filters={"ended_at": "is.null"},
    )
