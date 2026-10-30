"""Prerecorded voicemail drop for the Vonage sales dialer.

Call shape (see ``providers/vonage/routes.py``): the rep's browser places a
Client SDK ``serverCall``; Vonage asks our signed answer webhook for an NCCO
and we ``connect`` the rep's leg to the prospect's phone. That PSTN leg is
server-controllable through the Voice API, so a voicemail drop works like this:

1. The ``connect`` action asks for advanced machine detection
   (``mode=detect_beep``, ``behavior=continue``) and sends the PSTN leg's
   events to ``/vonage/dialer/events`` with an HMAC-bound context
   (dialer call id + the rep's own leg uuid).
2. On ``status=machine`` + ``sub_state=beep_start`` (greeting over) - or
   ``beep_timeout`` (no beep heard, treated like Twilio's machine_end_other) -
   one atomic database claim decides whether this call plays a recording.
3. The PSTN leg is transferred out of the rep's conversation to an NCCO of
   ``stream`` (the rep's effective WAV/MP3) + ``notify``. The rep's leg is
   hung up straight away, so the rep is free for the next lead while the
   message finishes in the background. When the NCCO runs out Vonage ends the
   PSTN leg itself.
4. The ``notify`` callback marks ``auto_voicemail_sent`` - unless it arrived
   provably before the recording could have finished (the file was not
   playable), in which case the failure is recorded instead. A PSTN
   ``completed`` event while playback was still running records a failure.

Never plays over a human: only an explicit machine classification (or a rep
press on a call whose account runs no detection) can start playback, and a
claim is a single conditional UPDATE so concurrent webhooks cannot both play.

Everything that talks to Supabase here is fail-soft (returns False/None),
mirroring dialer_call_log.py: a history write must never break a live call.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import jwt
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.providers.twilio import dialer_call_log

PROVIDER = "vonage"
_DIALER_CALLS = "/rest/v1/dialer_calls"
_VONAGE_API = "https://api.nexmo.com"

# Vonage's advanced machine detection. detect_beep waits for the greeting's
# beep, so the recording starts where a mailbox is actually recording. 45s is
# Vonage's minimum beep_timeout.
AMD_BEEP_TIMEOUT_SECONDS = 45

MACHINE_END_RESULTS = frozenset({"machine_end_beep", "machine_end_silence", "machine_end_other"})


# ─────────────────────────── signed callback context ───────────────────────────

def _context_signature(secret: str, fields: dict[str, str]) -> str:
    message = "&".join(f"{key}={fields[key]}" for key in sorted(fields))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def sign_context(secret: str, fields: dict[str, str]) -> str:
    """Query string for a callback URL whose parameters cannot be altered.

    Vonage's webhook JWT proves the request came from Vonage but does not cover
    the URL, so the identifiers we put in our own callback URLs carry an HMAC
    keyed by the application's signature secret.
    """
    clean = {key: str(value) for key, value in fields.items() if value not in (None, "")}
    return urlencode({**clean, "sig": _context_signature(secret, clean)})


def verify_context(secret: str, query: dict[str, str]) -> dict[str, str] | None:
    fields = {key: str(value) for key, value in query.items() if key != "sig"}
    supplied = str(query.get("sig") or "")
    if not secret or not supplied:
        return None
    expected = _context_signature(secret, fields)
    return fields if hmac.compare_digest(expected, supplied) else None


def payload_hash_matches(claims: dict, raw_body: bytes) -> bool:
    """Vonage signed webhooks carry SHA-256(body) as ``payload_hash``.

    When the claim is present it must match, which stops a captured JWT being
    replayed with a different body inside its validity window.
    """
    claimed = claims.get("payload_hash")
    if not claimed:
        return True
    return hmac.compare_digest(str(claimed).lower(), hashlib.sha256(raw_body or b"").hexdigest())


# ─────────────────────────────── Vonage Voice API ───────────────────────────────

async def _vonage_credentials() -> dict:
    from api.db import db_client

    saved = await db_client.get_platform_vonage_dialer_credentials() or {}
    return {
        "application_id": (saved.get("application_id") or os.getenv("VONAGE_DIALER_APPLICATION_ID") or "").strip(),
        "private_key": saved.get("private_key") or os.getenv("VONAGE_DIALER_PRIVATE_KEY") or "",
        "signature_secret": saved.get("signature_secret") or os.getenv("VONAGE_DIALER_SIGNATURE_SECRET") or "",
    }


async def signature_secret() -> str:
    try:
        return (await _vonage_credentials())["signature_secret"]
    except Exception as exc:  # noqa: BLE001 - callers fail closed on an empty secret
        logger.error(f"Could not load the Vonage dialer signature secret: {type(exc).__name__}")
        return ""


def _application_jwt(application_id: str, private_key: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"application_id": application_id, "iat": now, "nbf": now, "exp": now + 300, "jti": str(uuid4())},
        private_key.replace("\\n", "\n"),
        algorithm="RS256",
    )


async def _modify_call(uuid: str, body: dict) -> bool:
    try:
        credentials = await _vonage_credentials()
        if not credentials["application_id"] or not credentials["private_key"]:
            logger.error("Vonage dialer application credentials are missing; cannot control the call")
            return False
        token = _application_jwt(credentials["application_id"], credentials["private_key"])
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.put(
                f"{_VONAGE_API}/v1/calls/{uuid}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code in (200, 204):
            return True
        logger.error(f"Vonage rejected {body.get('action')} on {uuid}: HTTP {response.status_code}")
        return False
    except Exception as exc:  # noqa: BLE001 - caller decides how to recover
        logger.error(f"Vonage {body.get('action')} on {uuid} failed: {type(exc).__name__}")
        return False


async def transfer_to_ncco(uuid: str, ncco: list[dict]) -> bool:
    return await _modify_call(uuid, {"action": "transfer", "destination": {"type": "ncco", "ncco": ncco}})


async def hangup_leg(uuid: str) -> bool:
    return await _modify_call(uuid, {"action": "hangup"})


async def find_other_leg(child_uuid: str) -> str | None:
    """The rep's leg sharing a conversation with the prospect's PSTN leg.

    Used by the one-tap path, whose request does not carry the rep's leg id.
    Must run BEFORE the transfer moves the PSTN leg to a new conversation.
    """
    try:
        credentials = await _vonage_credentials()
        if not credentials["application_id"] or not credentials["private_key"]:
            return None
        headers = {"Authorization": f"Bearer {_application_jwt(credentials['application_id'], credentials['private_key'])}"}
        async with httpx.AsyncClient(timeout=6.0) as client:
            leg = await client.get(f"{_VONAGE_API}/v1/calls/{child_uuid}", headers=headers)
            leg.raise_for_status()
            conversation_uuid = (leg.json() or {}).get("conversation_uuid")
            if not conversation_uuid:
                return None
            listed = await client.get(
                f"{_VONAGE_API}/v1/calls",
                params={"conversation_uuid": conversation_uuid, "status": "answered"},
                headers=headers,
            )
            listed.raise_for_status()
            calls = ((listed.json() or {}).get("_embedded") or {}).get("calls") or []
    except Exception as exc:  # noqa: BLE001 - the rep can still hang up themselves
        logger.warning(f"Could not find the rep's Vonage leg for {child_uuid}: {type(exc).__name__}")
        return None
    others = [str(call.get("uuid")) for call in calls if isinstance(call, dict) and call.get("uuid") != child_uuid]
    return others[0] if len(others) == 1 and _is_uuid(others[0]) else None


def playback_ncco(*, playback_url: str, complete_url: str, dialer_call_id: str) -> list[dict]:
    """Play the message once, then report; the leg ends when the NCCO runs out."""
    return [
        {"action": "stream", "streamUrl": [playback_url], "bargeIn": False, "loop": 1},
        {
            "action": "notify",
            "payload": {"dialer_call_id": dialer_call_id, "event": "voicemail_played"},
            "eventUrl": [complete_url],
            "eventMethod": "POST",
        },
    ]


# ─────────────────────────────── dialer_calls rows ───────────────────────────────

def _headers(*, representation: bool = False) -> dict:
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if representation:
        headers["Prefer"] = "return=representation"
    return headers


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def get_call(dialer_call_id: str) -> dict | None:
    """The Vonage dialer_calls row for this browser-generated call id, or None."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not _is_uuid(dialer_call_id):
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}{_DIALER_CALLS}",
                params={
                    "select": (
                        "parent_call_sid,child_call_sid,rep_user_id,entry_id,provider,from_number,"
                        "to_number,status,ended_at,amd_result,manual_voicemail_requested_at,"
                        "voicemail_drop_started_at,auto_voicemail_sent,voicemail_drop_id,"
                        "voicemail_drop_failed_at,voicemail_drop_failure,created_at"
                    ),
                    "parent_call_sid": f"eq.{dialer_call_id}",
                    "provider": f"eq.{PROVIDER}",
                    "limit": "1",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - unknown is treated as missing
        logger.error(f"Could not read Vonage dialer call {dialer_call_id}: {type(exc).__name__}")
        return None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


async def _conditional_patch(filters: dict[str, str], payload: dict) -> int:
    """One conditional UPDATE; returns the number of rows it changed (0 on error).

    A single PostgREST PATCH is a single UPDATE, so the WHERE clause is the
    atomic guard: two racing webhooks cannot both pass ``... is null``.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return 0
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS}",
                params={"provider": f"eq.{PROVIDER}", **filters},
                json=payload,
                headers=_headers(representation=True),
            )
            response.raise_for_status()
            rows = response.json() if response.content else []
        return len(rows) if isinstance(rows, list) else 0
    except Exception as exc:  # noqa: BLE001 - fail-soft history write
        logger.error(f"Vonage dialer_calls update failed: {type(exc).__name__}")
        return 0


async def bind_child_leg(*, dialer_call_id: str, child_uuid: str) -> bool:
    """Bind the prospect's PSTN leg to the call row once; later events must match it."""
    if not _is_uuid(child_uuid):
        return False
    changed = await _conditional_patch(
        {"parent_call_sid": f"eq.{dialer_call_id}", "child_call_sid": "is.null"},
        {"child_call_sid": child_uuid},
    )
    if changed:
        return True
    row = await get_call(dialer_call_id)
    return bool(row and row.get("child_call_sid") == child_uuid)


async def record_amd_result(*, dialer_call_id: str, child_uuid: str, amd_result: str) -> bool:
    """Persist detection without ever downgrading a later, stronger result."""
    filters = {"parent_call_sid": f"eq.{dialer_call_id}", "child_call_sid": f"eq.{child_uuid}"}
    if amd_result in MACHINE_END_RESULTS:
        filters["or"] = "(amd_result.is.null,amd_result.eq.machine_start)"
    elif amd_result in {"machine_start", "human", "unknown"}:
        filters["amd_result"] = "is.null"
    else:
        return False
    return await _conditional_patch(filters, {"amd_result": amd_result, "amd_decided_at": _now()}) > 0


async def request_manual_drop(*, dialer_call_id: str, rep_user_id: str) -> bool:
    return await _conditional_patch(
        {
            "parent_call_sid": f"eq.{dialer_call_id}",
            "rep_user_id": f"eq.{rep_user_id}",
            "ended_at": "is.null",
            "manual_voicemail_requested_at": "is.null",
            "voicemail_drop_started_at": "is.null",
            "auto_voicemail_sent": "is.false",
        },
        {"manual_voicemail_requested_at": _now()},
    ) > 0


async def claim_playback(
    *, dialer_call_id: str, child_uuid: str, drop_id: str, manual: bool, require_machine_end: bool = True,
) -> bool:
    """Exactly one caller may start playback on a call, and never twice."""
    filters = {
        "parent_call_sid": f"eq.{dialer_call_id}",
        "child_call_sid": f"eq.{child_uuid}",
        "ended_at": "is.null",
        "voicemail_drop_started_at": "is.null",
        "voicemail_drop_failed_at": "is.null",
        "auto_voicemail_sent": "is.false",
        "manual_voicemail_requested_at": "not.is.null" if manual else "is.null",
    }
    if require_machine_end:
        filters["amd_result"] = "in.(" + ",".join(sorted(MACHINE_END_RESULTS)) + ")"
    else:
        # Rep-confirmed drop on an account without detection: still refuse a
        # call that detection (if any ran) classified as a person or unknown.
        filters["amd_result"] = "is.null"
    payload: dict[str, Any] = {"voicemail_drop_started_at": _now()}
    if drop_id:
        payload["voicemail_drop_id"] = drop_id
    return await _conditional_patch(filters, payload) > 0


async def mark_sent(*, dialer_call_id: str, child_uuid: str) -> bool:
    return await _conditional_patch(
        {
            "parent_call_sid": f"eq.{dialer_call_id}",
            "child_call_sid": f"eq.{child_uuid}",
            "voicemail_drop_started_at": "not.is.null",
            "voicemail_drop_failed_at": "is.null",
            "auto_voicemail_sent": "is.false",
        },
        {"auto_voicemail_sent": True},
    ) > 0


async def mark_failed(*, dialer_call_id: str, reason: str, child_uuid: str | None = None) -> bool:
    filters = {
        "parent_call_sid": f"eq.{dialer_call_id}",
        "voicemail_drop_failed_at": "is.null",
        "auto_voicemail_sent": "is.false",
    }
    if child_uuid:
        filters["child_call_sid"] = f"eq.{child_uuid}"
    return await _conditional_patch(
        filters, {"voicemail_drop_failed_at": _now(), "voicemail_drop_failure": reason[:120]},
    ) > 0


# ─────────────────────────────── policy resolution ───────────────────────────────

async def rep_auto_sends(*, rep_user_id: str, dialer_call_id: str, entry_id: str | None, policy: dict | None) -> bool:
    """Whether a confirmed machine on this rep's single call is dropped automatically.

    Uses the shared server-side resolver ``dialer_call_log.effective_auto_send``
    (the rep's own "send automatically" choice, else the team default; the old
    team ``auto_drop_enabled`` switch before migration 20270421110000), plus the
    team's on-machine action, exactly as the Twilio answer path does. The Vonage
    dialer has no parallel mode, so ``parallel`` is always False here.
    If that helper is absent (older backend), the team setting decides.
    """
    if not isinstance(policy, dict):
        return False
    if str(policy.get("on_machine") or "drop_vm") != "drop_vm":
        return False
    shared = getattr(dialer_call_log, "effective_auto_send", None)
    if callable(shared):
        try:
            return shared(policy, parallel=False) is True
        except Exception as exc:  # noqa: BLE001 - optional automation fails closed
            logger.warning(f"Shared auto-voicemail resolver failed for {dialer_call_id}: {type(exc).__name__}")
            return False
    return policy.get("auto_drop_enabled") is True


async def resolve_playback(*, rep_user_id: str, dialer_call_id: str, entry_id: str | None) -> tuple[str, str | None] | None:
    """(signed playback url, drop id) for a provider-playable recording, or None."""
    _enabled, url, drop_id = await dialer_call_log.get_auto_voicemail_settings(
        user_id=rep_user_id,
        allow_manual_when_disabled=True,
        parent_call_sid=dialer_call_id,
        entry_id=entry_id,
    )
    # Vonage `stream` plays MP3 or 16-bit WAV only; never report a browser
    # WebM/Opus recording as sent.
    if not url or not dialer_call_log.is_provider_playable_voicemail(url):
        return None
    return url, drop_id


def completion_query(
    secret: str, *, dialer_call_id: str, child_uuid: str, drop_id: str | None,
) -> str:
    fields = {"call": dialer_call_id, "leg": child_uuid}
    expected = dialer_call_log.voicemail_duration(drop_id)
    if expected:
        fields["vm_started"] = str(int(time.time() * 1000))
        fields["vm_expect"] = str(int(expected * 1000))
    return sign_context(secret, fields)


def playback_finished(fields: dict[str, str]) -> bool:
    """False only when the callback provably arrived before the recording could end."""
    try:
        started_ms = int(fields.get("vm_started") or 0)
        expect_ms = int(fields.get("vm_expect") or 0)
    except ValueError:
        return True
    if started_ms <= 0 or expect_ms <= 0:
        return True
    elapsed_ms = int(time.time() * 1000) - started_ms
    return elapsed_ms >= int(expect_ms * 0.85) - 500


async def start_playback(
    *,
    endpoint_root: str,
    secret: str,
    row: dict,
    child_uuid: str,
    rep_leg_uuid: str | None,
    manual: bool,
    require_machine_end: bool = True,
) -> str:
    """Claim, divert the prospect's leg to the recording, release the rep.

    Returns "playing", "unavailable" (no playable recording), "not_claimed"
    (another path already started/finished it, or the call is not eligible) or
    "failed" (Vonage refused the transfer; recorded, the rep stays connected).
    """
    dialer_call_id = str(row["parent_call_sid"])
    rep_user_id = str(row.get("rep_user_id") or "")
    entry_id = row.get("entry_id")
    resolved = await resolve_playback(rep_user_id=rep_user_id, dialer_call_id=dialer_call_id, entry_id=entry_id)
    if not resolved:
        return "unavailable"
    playback_url, drop_id = resolved
    if not await claim_playback(
        dialer_call_id=dialer_call_id, child_uuid=child_uuid, drop_id=drop_id or "",
        manual=manual, require_machine_end=require_machine_end,
    ):
        return "not_claimed"
    complete_url = f"{endpoint_root}/vonage/dialer/voicemail-complete?" + completion_query(
        secret, dialer_call_id=dialer_call_id, child_uuid=child_uuid, drop_id=drop_id,
    )
    ncco = playback_ncco(playback_url=playback_url, complete_url=complete_url, dialer_call_id=dialer_call_id)
    if not await transfer_to_ncco(child_uuid, ncco):
        await mark_failed(dialer_call_id=dialer_call_id, child_uuid=child_uuid, reason="provider_transfer_failed")
        return "failed"
    # The prospect's leg is now in its own conversation playing the message.
    # End the rep's leg so the browser is free for the next lead at once.
    if rep_leg_uuid and _is_uuid(rep_leg_uuid):
        if not await hangup_leg(rep_leg_uuid):
            logger.warning(f"Could not release the rep's Vonage leg on {dialer_call_id}; the rep can hang up")
    return "playing"


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def normalize_e164(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


# ─────────────────────────────── webhook orchestration ───────────────────────────────

ENDED_STATUSES = frozenset({"completed", "failed", "rejected", "busy", "timeout", "cancelled", "unanswered"})


def classify_amd_event(status: str, sub_state: str | None) -> str | None:
    """Map Vonage advanced machine detection onto dialer_calls.amd_result."""
    status = (status or "").lower()
    sub_state = (sub_state or "").lower()
    if status == "human":
        return "human"
    if status != "machine":
        return None
    if sub_state == "beep_start":
        return "machine_end_beep"
    if sub_state == "beep_timeout":
        # No beep inside beep_timeout: the greeting is over; same treatment as
        # Twilio's machine_end_other, which the dialer already drops on.
        return "machine_end_other"
    return "machine_start"


async def answer_connect_options(
    *, endpoint_root: str, dialer_call_id: Any, rep_leg_uuid: Any, to_number: str, from_number: str,
) -> dict:
    """Extra `connect` fields for a call we can track; {} keeps the plain connect.

    The context is bound only when the browser's call id matches a live,
    unanswered Vonage history row with the same destination and caller ID.
    """
    if not _is_uuid(dialer_call_id) or not _is_uuid(rep_leg_uuid):
        return {}
    dialer_call_id = str(dialer_call_id)
    row = await get_call(dialer_call_id)
    if (
        not row
        or row.get("ended_at")
        or row.get("child_call_sid")
        or normalize_e164(row.get("to_number")) != normalize_e164(to_number)
        or normalize_e164(row.get("from_number")) != normalize_e164(from_number)
    ):
        logger.warning(f"Vonage answer for {dialer_call_id} does not match its history row; no voicemail tracking")
        return {}
    secret = await signature_secret()
    if not secret:
        return {}
    options: dict[str, Any] = {
        "eventUrl": [f"{endpoint_root}/vonage/dialer/events?" + sign_context(
            secret, {"call": dialer_call_id, "rep": str(rep_leg_uuid)},
        )],
        "eventMethod": "POST",
    }
    policy = await dialer_call_log.get_team_voicemail_policy(
        user_id=str(row.get("rep_user_id") or ""), parent_call_sid=dialer_call_id, entry_id=row.get("entry_id"),
    )
    if _detection_wanted(policy):
        options["advancedMachineDetection"] = {
            "behavior": "continue",
            "mode": "detect_beep",
            "beep_timeout": AMD_BEEP_TIMEOUT_SECONDS,
        }
    return options


async def on_machine_end(*, endpoint_root: str, secret: str, dialer_call_id: str, child_uuid: str, rep_leg_uuid: str | None) -> str:
    row = await get_call(dialer_call_id)
    if not row or row.get("child_call_sid") != child_uuid or row.get("ended_at"):
        return "ignored"
    rep_user_id = str(row.get("rep_user_id") or "")
    manual = bool(row.get("manual_voicemail_requested_at"))
    policy = await dialer_call_log.get_team_voicemail_policy(
        user_id=rep_user_id, parent_call_sid=dialer_call_id, entry_id=row.get("entry_id"),
    )
    auto = await rep_auto_sends(
        rep_user_id=rep_user_id, dialer_call_id=dialer_call_id, entry_id=row.get("entry_id"), policy=policy,
    )
    if manual or auto:
        result = await start_playback(
            endpoint_root=endpoint_root, secret=secret, row=row, child_uuid=child_uuid,
            rep_leg_uuid=rep_leg_uuid, manual=manual,
        )
        if result != "unavailable":
            return result
        logger.warning(f"No WAV/MP3 recording available for Vonage call {dialer_call_id}; rep stays on the line")
        return "unavailable"
    if str((policy or {}).get("on_machine") or "") == "hangup":
        # Team policy: never leave a rep listening to a mailbox.
        await hangup_leg(child_uuid)
        return "hangup"
    # One-tap (or pass_to_rep): the rep is already hearing the mailbox and can
    # press Drop voicemail, which plays immediately now the beep is confirmed.
    return "rep_decides"


async def on_leg_ended(*, dialer_call_id: str, child_uuid: str) -> str:
    """The prospect's leg ended; settle a playback that never reported back."""
    row = await get_call(dialer_call_id)
    if (
        not row
        or row.get("child_call_sid") != child_uuid
        or not row.get("voicemail_drop_started_at")
        or row.get("auto_voicemail_sent")
        or row.get("voicemail_drop_failed_at")
    ):
        return "ignored"
    expected = dialer_call_log.voicemail_duration(row.get("voicemail_drop_id"))
    try:
        started = datetime.fromisoformat(str(row["voicemail_drop_started_at"]).replace("Z", "+00:00"))
    except ValueError:
        return "ignored"
    if not expected:
        # Without the recording length we cannot judge; the notify callback is
        # the authority and this event may simply have overtaken it.
        return "ignored"
    elapsed = (datetime.now(UTC) - started).total_seconds()
    if elapsed >= expected * 0.85 - 0.5:
        # The machine hung up after the whole message: notify can lose this race.
        await mark_sent(dialer_call_id=dialer_call_id, child_uuid=child_uuid)
        return "sent"
    await mark_failed(dialer_call_id=dialer_call_id, child_uuid=child_uuid, reason="call_ended_during_playback")
    return "failed"


def drop_state(row: dict | None) -> str:
    if not row:
        return "idle"
    if row.get("auto_voicemail_sent") is True:
        return "sent"
    if row.get("voicemail_drop_failed_at"):
        return "failed"
    if row.get("voicemail_drop_started_at"):
        return "playing"
    if row.get("manual_voicemail_requested_at"):
        return "waiting"
    return "idle"


async def request_rep_drop(*, endpoint_root: str, dialer_call_id: str, rep_user_id: str) -> tuple[str, str | None]:
    """One-tap drop from the rep. Returns (state, refusal reason)."""
    row = await get_call(dialer_call_id)
    if not row or str(row.get("rep_user_id") or "") != rep_user_id:
        return "idle", "not_found"
    state = drop_state(row)
    if state in {"sent", "failed", "playing"}:
        return state, None
    if row.get("ended_at"):
        return state, "call_ended"
    child_uuid = row.get("child_call_sid")
    amd_result = row.get("amd_result")
    if not child_uuid:
        return state, "not_answered"
    if amd_result in {"human", "unknown", "fax"}:
        return state, "not_voicemail"
    secret = await signature_secret()
    if not secret:
        return state, "unavailable"
    if amd_result == "machine_start" or (amd_result is None and await _detection_running(row)):
        # Detection is still listening for the beep; the beep event plays it.
        await request_manual_drop(dialer_call_id=dialer_call_id, rep_user_id=rep_user_id)
        return "waiting", None
    await request_manual_drop(dialer_call_id=dialer_call_id, rep_user_id=rep_user_id)
    rep_leg = await find_other_leg(str(child_uuid))
    refreshed = await get_call(dialer_call_id) or row
    result = await start_playback(
        endpoint_root=endpoint_root, secret=secret, row=refreshed, child_uuid=str(child_uuid),
        rep_leg_uuid=rep_leg, manual=True,
        # Machine end confirmed by Vonage, or no detection on this account and
        # the rep has heard the mailbox themselves (as on Twilio).
        require_machine_end=amd_result is not None,
    )
    if result == "playing":
        return "playing", None
    if result == "unavailable":
        return "idle", "no_playable_recording"
    if result == "failed":
        return "failed", "provider_transfer_failed"
    return drop_state(await get_call(dialer_call_id)), "not_claimed"


async def _detection_running(row: dict) -> bool:
    policy = await dialer_call_log.get_team_voicemail_policy(
        user_id=str(row.get("rep_user_id") or ""),
        parent_call_sid=str(row.get("parent_call_sid") or ""),
        entry_id=row.get("entry_id"),
    )
    # Unknown policy: assume detection may be running, which only ever delays
    # (never forces) playback.
    return policy is None or _detection_wanted(policy)


def _detection_wanted(policy: dict | None) -> bool:
    """Vonage AMD is billed per call: request it only when something uses it."""
    return isinstance(policy, dict) and any(
        policy.get(key) is True for key in ("machine_detection_enabled", "auto_drop_enabled", "single_auto_send")
    )
