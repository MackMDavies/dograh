"""Voicemail / screening stages of a dialer call, the recording lock and the ring-back.

Provider-neutral Supabase plumbing for Dialer Phase 1 (migrations 20270424*):

* ``record_call_stage`` appends to ``dialer_call_stage_events`` (service role). The key
  is the id the browser polls with: the Twilio parent call SID, or the SignalWire rep
  call id of a one-line screened call (a parallel line uses its own provider call id).
* ``latest_call_stage`` feeds ``stage`` / ``stage_at`` into the voicemail status polls.
* ``report_listening_stage`` is the hook for the live listening stream / call sorter.
* ``voicemail_recording_required`` is the server half of the recording lock.
* ``offer_voicemail_ringback`` / ``record_ringback_joined`` ring the original rep back
  when a person answers after they pressed "Send voicemail" and moved on.

Same contract as dialer_call_log: nothing here raises into a webhook. Stage writes are
evidence for the rep's screen and history, never a reason to change a live call.
"""

from typing import Any

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

STAGES = frozenset({
    "ringing", "vm_button_shown", "vm_requested", "machine", "beep", "vm_playing",
    "vm_left", "vm_live_recording", "vm_left_live", "person", "mailbox_full",
    "no_answer", "screener", "ivr", "rep_ringback", "rep_rejoined",
    # Phase 1b: the extra-answer chain (written in SQL by dialer_extra_answer_*).
    "extra_answer", "teammate_connected", "agent_bridging", "agent_transferred",
    "callback_booked", "apology_played",
})

# What the listening stream / call sorter may report (see report_listening_stage).
LISTENING_STAGES = frozenset({"screener", "ivr", "mailbox_full", "machine", "person"})

# A playback the carrier ended within this long of starting never reached a mailbox
# that was recording: the mailbox is full (or not set up) and hung up on us.
MAILBOX_FULL_MAX_PLAYBACK_MS = 3000

RECORDING_REQUIRED_CODE = "voicemail_recording_required"
RECORDING_REQUIRED_MESSAGE = "Record your voicemail to start calling."

# The original rep's softphone auto-answers the ring-back (Phase 1b); after this long
# without a join the extra-answer chain moves on to a teammate.
RINGBACK_OFFER_SECONDS = 10


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


async def _rpc(name: str, payload: dict[str, Any], *, timeout: float = 5.0) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=payload, headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


def _valid_key(call_key: str | None) -> bool:
    return isinstance(call_key, str) and 8 <= len(call_key) <= 128


async def record_call_stage(
    call_key: str | None, stage: str, detail: dict[str, Any] | None = None,
) -> bool:
    """Append one stage for this call. A repeat of the latest stage is ignored in SQL."""
    if stage not in STAGES or not _valid_key(call_key) or not _ready():
        return False
    try:
        result = await _rpc("dialer_record_call_stage", {
            "p_parent_call_sid": call_key,
            "p_stage": stage,
            "p_detail": detail or {},
        })
        return isinstance(result, str) and bool(result)
    except Exception as exc:  # noqa: BLE001 - evidence only; never affects the call
        logger.info(f"Could not record dialer stage {stage} for {call_key}: {type(exc).__name__}")
        return False


async def latest_call_stage(call_key: str | None) -> dict[str, Any] | None:
    """{stage, stage_at, detail} of the newest stage for this call, or None."""
    if not _valid_key(call_key) or not _ready():
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_call_stage_events",
                params={
                    "select": "stage,detail,created_at",
                    "parent_call_sid": f"eq.{call_key}",
                    "order": "created_at.desc",
                    "limit": "1",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - optional field on a status poll
        logger.info(f"Dialer stage unavailable for {call_key}: {type(exc).__name__}")
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if row.get("stage") not in STAGES:
        return None
    return {
        "stage": row["stage"],
        "stage_at": row.get("created_at"),
        "detail": row.get("detail") if isinstance(row.get("detail"), dict) else {},
    }


# The person is with (or being connected to) someone: the original rep on a ring-back,
# or the Phase 1b chain's teammate / voice agent. Never "picked up after you moved on".
_LOOKED_AFTER_STAGES = frozenset({
    "rep_ringback", "rep_rejoined", "extra_answer", "teammate_connected",
    "agent_bridging", "agent_transferred",
})


def with_stage(payload: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    """Add `stage` / `stage_at` to a voicemail status payload.

    While the original rep is being rung back (or has rejoined) the drop state reads
    `ringback`, so the browser does not report "picked up after you moved on" for a
    call the rep is about to take.
    """
    payload["stage"] = (latest or {}).get("stage")
    payload["stage_at"] = (latest or {}).get("stage_at")
    if payload["stage"] in _LOOKED_AFTER_STAGES and payload.get("state") in {
        "rejected", "waiting", "idle",
    }:
        payload["state"] = "ringback"
        payload.pop("failure", None)
    return payload


async def report_listening_stage(
    *, call_key: str, stage: str, detail: dict[str, Any] | None = None,
) -> bool:
    """Hook for the live listening stream / call sorter.

    ``stage`` is one of 'screener', 'ivr', 'mailbox_full', 'machine', 'person'. It is
    recorded against the same key the browser polls (Twilio parent call SID or the
    SignalWire rep call id), so the rep's status line updates on the next poll.

    'mailbox_full' on a Twilio call also marks the queued/playing voicemail as failed
    with reason ``mailbox_full``, so the call is never reported as "voicemail left".
    """
    if stage not in LISTENING_STAGES:
        raise ValueError(f"unsupported listening stage: {stage!r}")
    recorded = await record_call_stage(call_key, stage, {"source": "listening", **(detail or {})})
    if stage == "mailbox_full" and call_key.startswith("CA"):
        from api.services.telephony.providers.twilio.dialer_call_log import (
            record_voicemail_playback_failure,
        )
        await record_voicemail_playback_failure(parent_call_sid=call_key, reason="mailbox_full")
    return recorded


async def voicemail_recording_required(rep_user_id: str | None) -> bool:
    """True when this rep must record a default voicemail before dialing out.

    Fails open (False) when readiness cannot be read: the same rule is enforced in SQL
    on the call/session insert, so an outage here never blocks a rep who may dial.
    """
    if not rep_user_id or not _ready():
        return False
    try:
        result = await _rpc("dialer_user_voicemail_readiness", {"p_user_id": rep_user_id}, timeout=3.0)
    except Exception as exc:  # noqa: BLE001 - SQL guard is the second line
        logger.warning(f"Could not read voicemail readiness for {rep_user_id}: {type(exc).__name__}")
        return False
    return isinstance(result, dict) and result.get("lock") is True


def recording_required_detail() -> dict[str, str]:
    """HTTP error body for a JSON start-call endpoint refused by the lock."""
    return {"code": RECORDING_REQUIRED_CODE, "message": RECORDING_REQUIRED_MESSAGE}


async def offer_voicemail_ringback(
    *, provider: str, parent_call_sid: str, prospect_call_id: str, conference_name: str,
) -> dict[str, Any] | None:
    """Ring the original rep for a person who answered after a voicemail hand-off.

    Returns {inbound_call_id, rep_user_id, conference_name} when the rep is free and the
    inbound offer was written (stages 'person' + 'rep_ringback' are recorded in the same
    transaction); None when the rep is busy/offline or anything is uncertain.
    """
    if not _ready():
        return None
    try:
        result = await _rpc("dialer_offer_voicemail_ringback", {
            "p_provider": provider,
            "p_parent_call_sid": parent_call_sid,
            "p_prospect_call_id": prospect_call_id,
            "p_conference_name": conference_name,
        })
    except Exception as exc:  # noqa: BLE001 - no offer means today's behaviour
        logger.warning(f"Could not offer a ring-back for {parent_call_sid}: {type(exc).__name__}")
        return None
    if isinstance(result, dict) and isinstance(result.get("inbound_call_id"), str):
        return result
    return None


async def record_ringback_joined(conference_name: str) -> bool:
    """Record 'rep_rejoined' when the rep's leg joins a ring-back conference (no-op otherwise)."""
    if not conference_name or not _ready():
        return False
    try:
        return await _rpc("dialer_record_voicemail_ringback_joined", {
            "p_conference_name": conference_name,
        }) is True
    except Exception as exc:  # noqa: BLE001 - evidence only
        logger.info(f"Could not record ring-back join for {conference_name}: {type(exc).__name__}")
        return False
