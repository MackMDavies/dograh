"""Twilio telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
import os
import re
import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, WebSocket
import httpx
from loguru import logger
from pipecat.utils.run_context import set_current_run_id
from pydantic import BaseModel, Field
from typing import Any, Literal
from uuid import UUID
from starlette.responses import HTMLResponse
from twilio.request_validator import RequestValidator

from api.db import db_client
from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.dialer.parallel_auth import issue_parallel_auth_token
from api.services.telephony.dialer import call_handling, call_sorter, call_stages, extra_answer, sorter_gate, twilio_live_transcribe
from api.services.telephony.dialer.inbound_call_log import (
    bind_inbound_rep_leg,
    close_inbound_call,
    evaluate_overflow_guard,
    expire_unanswered_inbound_call,
    is_claimed_inbound_conference,
    notify_inbound_call,
    record_overflow_join,
)
from api.services.telephony.dialer.compliance import (
    BATCH_TOO_SMALL_MESSAGE,
    dial_block_message,
    phone_block_reason,
    rep_overrides_hours,
    snap_parallel_lines,
)
from api.services.telephony.dialer.provider import (
    UnknownDialerProvider,
    get_dialer_provider,
    resolve_active_dialer_provider,
    resolve_configured_dialer_provider,
)
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.providers.twilio.floor_monitor import (
    claim_floor_monitor,
    change_floor_monitor_mode,
    close_floor_monitor_for_provider,
)
from api.services.telephony.providers.twilio.dialer_call_log import (
    create_dialer_call,
    consume_double_dial_authorization,
    check_dialer_calling_hours,
    check_dialer_calling_hours_with_state,
    get_auto_voicemail_settings,
    attach_voicemail_drop_to_call,
    get_dialer_phone_block_reason,
    get_team_voicemail_policy,
    get_dialer_call_child_leg,
    mark_auto_voicemail_sent,
    mark_rep_detached_for_voicemail,
    get_voicemail_drop_evidence,
    record_twilio_amd_decision,
    record_voicemail_playback_failure,
    settle_interrupted_playback,
    voicemail_duration,
    update_dialer_call_amd_result,
    request_manual_voicemail_drop,
    claim_manual_voicemail_playback,
    release_manual_voicemail_playback,
    mark_dialer_lead_joined_conference,
    update_dialer_call_child_sid,
    update_dialer_call_conference_sid,
    update_dialer_call_recording_by_conference_sid,
    update_dialer_call_recording_by_parent_call_sid,
    update_dialer_call_status,
)
from api.services.telephony.providers.twilio.dialer_conference import (
    _twilio_client,
    cancel_call,
    cancel_call_if_ringing,
    conference_name_for,
    dial_lead_into_conference,
    parent_call_sid_from_conference_name,
)
from api.services.telephony.providers.twilio import dialer_parallel
from api.services.telephony.providers.twilio import dialer_agent_handoff
from api.services.telephony.providers.twilio import dialer_extra_answer
from api.services.telephony.providers.twilio import dialer_hold
from api.services.telephony.providers.twilio.dialer_twiml import conference_telemetry_attributes as _conference_telemetry_attributes
from api.services.telephony.providers.twilio import dialer_overflow_recovery
from api.services.telephony.providers.twilio import dialer_voicemail_ringback
from api.services.telephony.providers.twilio import dialer_call_sorter
from api.services.telephony.providers.twilio.dialer_number_assignment import (
    DialerNumberAssignmentUnavailable,
    DialerNumberProviderMismatch,
    _parse_rep_id_from_identity,
    is_manager_or_admin,
    parallel_caller_id_candidates,
    choose_parallel_caller_ids,
    resolve_assigned_caller_id,
    resolve_assigned_dialer_number,
    user_owns_dialer_number,
)
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)
from api.utils.common import get_backend_endpoints

router = APIRouter()


def _twilio_event_time(value: str | None) -> str | None:
    """Parse Twilio's signed RFC 2822 event timestamp as a UTC ISO value."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None

_PARALLEL_TERMINAL_LEG_STATES = frozenset({
    "completed", "busy", "no-answer", "failed", "cancelled", "abandoned_early",
})


def _parallel_refusal_twiml(message: str) -> str:
    """Hang up a refused parallel batch, telling the rep why in their headset.

    A bare <Hangup/> ended the browser call with no explanation, which the
    rep could not tell apart from a carrier fault.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Say>'
        + escape(message)
        + "</Say><Hangup/></Response>"
    )



def _dial_block_twiml(reason: str | None) -> str:
    """Tell the rep why a single dial was refused, then hang up (a bare <Hangup/> made the
    browser phone show "ConnectionError 31005", which reads like a carrier fault)."""
    return _parallel_refusal_twiml(dial_block_message(reason))


def _parallel_batch_has_settled(legs: list[dict]) -> bool:
    """Whether every parallel destination no longer needs the rep.

    A line already playing the rep's recording finishes by itself and records
    sent/failed on its own leg, so it counts as settled: the rep is released
    instead of sitting on hold music for the length of every message. (The
    browser's automatic next round still waits for those lines to be SENT -
    see dialer_parallel_batch_summary - so a prospect is never re-dialled
    while their voicemail is still being left.)
    """
    return bool(legs) and all(
        leg.get("status") in _PARALLEL_TERMINAL_LEG_STATES
        or (leg.get("status") == "voicemail" and leg.get("auto_voicemail_sent") is True)
        or (leg.get("status") == "voicemail" and bool(leg.get("voicemail_drop_started_at")))
        for leg in legs
    )


async def _get_assigned_dialer_number_or_503(rep_id: int):
    try:
        return await resolve_assigned_dialer_number(rep_id)
    except DialerNumberAssignmentUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not verify your assigned dialer number. Please try again shortly; no call was placed.",
        ) from exc

# Statuses at which issuing a cancel on the lead's leg is pointless: the leg
# either joined the conference (Twilio's own teardown ends it) or is already
# terminal. Anything else - including an unknown/missing status - is treated
# as "possibly still ringing" and cancelled; see dialer_conference.cancel_call.
#
# Deliberately a NEGATIVE set. A positive "is cancellable" set would make an
# unknown or missing status SKIP the cancel, which is the wrong way to fail
# for something that exists to prevent abandoned calls.
#
# Overlaps api.routes.telephony._TERMINAL_CALL_STATUSES - same Twilio
# vocabulary, different question ("can we stop polling?" vs "is a cancel
# pointless?"), so they are not identical and are kept separate rather than
# imported across the routes <-> provider boundary. If you edit one, read the
# other.
_LEAD_LEG_SETTLED_STATUSES = frozenset(
    {"in-progress", "answered", "completed", "busy", "no-answer", "canceled", "failed"}
)

# Twilio Call SID grammar: literal "CA" + 32 lowercase hex. Used to validate
# the one caller-supplied value that becomes a conference name - see
# _serve_listen_in_twiml.
_TWILIO_CALL_SID_PATTERN = re.compile(r"^CA[0-9a-f]{32}$")


class VoiceTokenResponse(BaseModel):
    token: str
    identity: str
    # Which SDK the browser should instantiate. Always populated from the
    # provider that actually minted the token, never from the requested name,
    # so the response cannot claim a provider that did not issue the creds.
    provider: str
    # Provider-specific dial target; "" for Twilio, which routes via the
    # TwiML App's fixed Voice URL. Defaulted so older callers keep working.
    destination: str = ""
    caller_number: str = ""
    parallel_auth_token: str | None = None
    # The browser uses this boundary to move to the provider selected by the
    # next rotation slot while idle. It is derived server-side from the same
    # atomic assignment RPC used before outbound calls.
    next_rotation_at: datetime | None = None


class TelnyxDialerCallStartRequest(BaseModel):
    call_id: UUID
    to_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    entry_id: UUID | None = None
    # The rep confirmed the two-step calling-hours warning.
    hours_override: bool = False


class TelnyxDialerCallStatusRequest(BaseModel):
    call_id: UUID
    status: Literal["ringing", "answered", "completed", "failed", "no-answer"]
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)


class VonageDialerCallStartRequest(TelnyxDialerCallStartRequest):
    pass


class VonageDialerCallStatusRequest(TelnyxDialerCallStatusRequest):
    pass


class TelnyxInboundCallRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=200)
    from_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    to_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    caller_name: str | None = Field(default=None, max_length=200)


class TelnyxInboundCallEndRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["completed", "failed", "missed"] = "completed"
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)


class TelnyxInboundCallAnswerRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=200)


class ManualVoicemailDropRequest(BaseModel):
    parent_call_sid: str = Field(pattern=r"^CA[0-9a-f]{32}$")
    action: Literal["send", "cancel", "self"] = "send"


@router.get("/voice-token")
async def get_voice_token(
    user=Depends(require_sales_dialer_role),
) -> VoiceTokenResponse:
    assigned = await _get_assigned_dialer_number_or_503(user.id)
    assigned_provider = str((assigned or {}).get("provider") or "").strip().lower()
    provider_name = (
        assigned_provider
        if assigned_provider in {"twilio", "signalwire", "telnyx", "vonage"}
        else await resolve_configured_dialer_provider()
    )
    try:
        provider = get_dialer_provider(provider_name)
        creds = await provider.mint_credentials(user_id=user.id)
    except UnknownDialerProvider as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any provider's "not configured"
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return VoiceTokenResponse(
        token=creds.token,
        identity=creds.identity,
        provider=provider.name,
        destination=creds.destination,
        caller_number=(assigned or {}).get("phone_number", "")
        if provider.name in {"telnyx", "vonage"}
        else "",
        parallel_auth_token=(
            issue_parallel_auth_token(str(user.provider_id))
            if provider.name == "signalwire"
            else None
        ),
        next_rotation_at=(assigned or {}).get("next_rotation_at"),
    )


_MACHINE_END_RESULTS = frozenset({"machine_end_beep", "machine_end_silence", "machine_end_other"})


def _amd_machine_stages(answered_by: str, *, mode: str | None) -> list[tuple[str, dict]]:
    """Stages for a mailbox handed to the rep, from Twilio's AMD answer.

    'machine' is the AMD decision itself. 'beep' only when there is one to report:
    machine_end_beep (Twilio heard the tone) or machine_end_silence (the greeting
    ended in silence, so the mailbox is recording). machine_end_other is AMD running
    out of time with no tone heard: the rep sees 'machine' only (the call sorter may
    still report a real beep later).
    """
    detail = {"answered_by": answered_by, "mode": mode}
    stages: list[tuple[str, dict]] = [("machine", {**detail, "source": "amd"})]
    if answered_by == "machine_end_beep":
        stages.append(("beep", {**detail, "source": "tone"}))
    elif answered_by == "machine_end_silence":
        stages.append(("beep", {**detail, "source": "end_of_greeting"}))
    return stages


def _manual_drop_state(call_row: dict | None) -> str:
    if not call_row:
        return "idle"
    if call_row.get("auto_voicemail_sent") is True:
        return "sent"
    if call_row.get("voicemail_drop_failed_at"):
        return "failed"
    if call_row.get("voicemail_drop_started_at"):
        return "playing"
    if not call_row.get("manual_voicemail_requested_at"):
        return "idle"
    amd_result = call_row.get("amd_result")
    if amd_result is None:
        # Joined without any AMD result: detection is off for this account, so
        # nothing will ever confirm a mailbox. The rep must press again once
        # they hear the beep (that press plays immediately).
        return "needs_rep" if call_row.get("lead_joined_at") else "waiting"
    return "rejected" if amd_result not in _MACHINE_END_RESULTS else "waiting"


def _manual_drop_ready(call_row: dict | None) -> bool:
    """A manual playback may start now: the rep is on the line and pressed it.

    Once the rep can hear the far end, their press IS the verdict, whatever Twilio's
    machine detection guessed. A greeting that opens like a person ("Hi, you've reached
    John...") comes back `human` or `unknown`, and refusing then was the 'Leave voicemail
    does nothing' reps reported (2026-10-21: 14 of one rep's answered calls were
    human/unknown). Before the rep joins, the request still waits for a machine verdict."""
    return bool(call_row and call_row.get("lead_joined_at"))


# Every machine_end_* result means Twilio has already judged the greeting over, so the mailbox is
# recording now. A pause here only records silence, and many carrier mailboxes hang up after a few
# seconds of it: a 6-second pause on machine_end_other/silence cut real drops short (2026-10-19,
# CA073838579c…: AMD hit its 30s limit, then 6s of silence, then the mailbox ended 7s into the
# message). Start the recording immediately.
_VOICEMAIL_START_PAUSE_SECONDS = 0


def _voicemail_complete_url(
    *, endpoint_root: str, parent_call_sid: str, parallel_leg_id: str = "",
    drop_id: str | None = None, pause_seconds: int = 0,
) -> str:
    """Completion callback carrying the signed playback expectation.

    Twilio signs the full callback URL, so vm_started/vm_expect cannot be
    forged. The completion handler uses them to refuse a "sent" when Twilio
    reached the Redirect without playing the whole recording (an unfetchable
    or unplayable file is skipped by <Play>, which falls straight through).
    """
    fields = {"parent_call_sid": parent_call_sid}
    if parallel_leg_id:
        fields["parallel_leg_id"] = parallel_leg_id
    expected = voicemail_duration(drop_id)
    if expected:
        fields["vm_started"] = str(int(datetime.now(UTC).timestamp() * 1000))
        fields["vm_expect"] = str(int((expected + max(0, pause_seconds)) * 1000))
    return f"{endpoint_root}/dialer-auto-voicemail-complete?" + urlencode(fields)


def _voicemail_playback_twiml(
    *, endpoint_root: str, parent_call_sid: str, playback_url: str, pause_seconds: int = 0,
    parallel_leg_id: str = "", drop_id: str | None = None, leg_call_sid: str = "",
) -> str:
    pause_seconds = max(0, min(10, int(pause_seconds)))
    # The call sorter's fork (if any) has done its job once the recording plays.
    stop_sorter = (
        dialer_call_sorter.stop_stream_twiml(leg_call_sid=leg_call_sid)
        if leg_call_sid and call_sorter.call_sorter_enabled() else ""
    )
    complete_url = _voicemail_complete_url(
        endpoint_root=endpoint_root, parent_call_sid=parent_call_sid,
        parallel_leg_id=parallel_leg_id, drop_id=drop_id, pause_seconds=pause_seconds,
    )
    pause = f'<Pause length="{pause_seconds}"/>' if pause_seconds else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{stop_sorter}{pause}<Play>" + escape(playback_url) + "</Play>"
        f"<Redirect method=\"POST\">{escape(complete_url)}</Redirect></Response>"
    )


def _playback_finished(*, vm_started: str, vm_expect: str) -> bool:
    """False only when the callback provably arrived before the recording could end."""
    try:
        started_ms = int(vm_started)
        expect_ms = int(vm_expect)
    except (TypeError, ValueError):
        return True
    if started_ms <= 0 or expect_ms <= 0:
        return True
    elapsed_ms = int(datetime.now(UTC).timestamp() * 1000) - started_ms
    # 15% slack for provider clock/transcoding plus half a second of jitter.
    return elapsed_ms >= int(expect_ms * 0.85) - 500


async def _release_rep_for_voicemail(parent_call_sid: str) -> None:
    """End the rep's own leg once the prospect's leg owns the voicemail.

    Called only after the one-shot playback claim succeeded (the row now has
    voicemail_drop_started_at), so the rep is free to move on while the
    prospect leg - an independent REST-originated call, not a child of the
    rep's leg - keeps playing. The conference-end that follows is told to keep
    that leg (_cancel_orphaned_lead_leg), and the signed completion callback
    still records sent/failed on the same dialer_calls row. Fail-soft: if this
    fails the completion handler ends the rep's leg as before.
    """
    try:
        if not await cancel_call(call_sid=parent_call_sid):
            logger.warning(f"Could not release the rep from {parent_call_sid} for a background voicemail")
    except Exception as exc:  # noqa: BLE001 - background task must not raise
        logger.error(f"Rep release failed for {parent_call_sid}: {exc}")


def _voicemail_playback_in_progress(row: dict | None) -> bool:
    """The prospect leg is playing the recording and has not finished yet."""
    return bool(
        row
        and row.get("voicemail_drop_started_at")
        and row.get("auto_voicemail_sent") is not True
    )


async def _redirect_twilio_child_to_voicemail(*, child_call_sid: str, twiml: str) -> bool:
    client = await _twilio_client()
    if not client:
        return False
    try:
        await asyncio.to_thread(client.calls(child_call_sid).update, twiml=twiml)
        return True
    except Exception as exc:  # noqa: BLE001 - caller handles retryable failure
        logger.error(f"Could not redirect Twilio call {child_call_sid} to voicemail: {exc}")
        return False


# Machine detection off + the rep pressed "Send voicemail" and left: how long the
# sorter is given to hear the greeting end (the line is held silently meanwhile).
_GREETING_WAIT_SECONDS = 30


def _greeting_wait_twiml() -> str:
    """Hold a mailbox silently while its greeting plays; the recording is redirected in
    as soon as the greeting ends. Bounded: an unconfirmed mailbox is hung up, never left."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Pause length="{_GREETING_WAIT_SECONDS + 5}"/><Hangup/></Response>'
    )


async def _play_queued_voicemail_after_greeting(
    *, parent_call_sid: str, child_call_sid: str, endpoint_root: str,
    wait_seconds: float = _GREETING_WAIT_SECONDS,
) -> str:
    """AMD off: play the rep's queued recording when the sorter hears the greeting end.

    Returns what happened ('played' | 'mailbox_full' | 'no_recording' | 'not_claimed' |
    'redirect_failed'). Background task, never raises.
    """
    try:
        state = await call_sorter.wait_for_sorter_stage(
            parent_call_sid, stages=("beep", "mailbox_full"), timeout=wait_seconds,
        )
        stage = (state or {}).get("stage")
        if stage == "mailbox_full":
            await record_voicemail_playback_failure(parent_call_sid=parent_call_sid, reason="mailbox_full")
            await cancel_call(call_sid=child_call_sid)
            return "mailbox_full"
        call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        rep_user_id = (call_row or {}).get("rep_user_id")
        if not isinstance(rep_user_id, str):
            await cancel_call(call_sid=child_call_sid)
            return "no_recording"
        _, playback_url, drop_id = await get_auto_voicemail_settings(
            user_id=rep_user_id, allow_manual_when_disabled=True, parent_call_sid=parent_call_sid,
        )
        if not playback_url or not playback_url.split("?", 1)[0].lower().endswith(".wav"):
            await record_voicemail_playback_failure(parent_call_sid=parent_call_sid, reason="no_recording")
            await cancel_call(call_sid=child_call_sid)
            return "no_recording"
        # The sorter stands in for AMD: record its verdict so the one-shot claim applies.
        await update_dialer_call_child_sid(parent_call_sid=parent_call_sid, child_call_sid=child_call_sid)
        await update_dialer_call_amd_result(
            parent_call_sid=parent_call_sid,
            answered_by="machine_end_beep" if stage == "beep" else "machine_end_other",
        )
        if not await claim_manual_voicemail_playback(
            parent_call_sid=parent_call_sid, child_call_sid=child_call_sid, manual=True,
        ):
            return "not_claimed"
        if drop_id:
            await attach_voicemail_drop_to_call(
                parent_call_sid=parent_call_sid, child_call_sid=child_call_sid, drop_id=drop_id,
            )
        twiml = _voicemail_playback_twiml(
            endpoint_root=endpoint_root, parent_call_sid=parent_call_sid,
            playback_url=playback_url, pause_seconds=_VOICEMAIL_START_PAUSE_SECONDS,
            drop_id=drop_id, leg_call_sid=child_call_sid,
        )
        if not await _redirect_twilio_child_to_voicemail(child_call_sid=child_call_sid, twiml=twiml):
            await release_manual_voicemail_playback(
                parent_call_sid=parent_call_sid, child_call_sid=child_call_sid,
            )
            await record_voicemail_playback_failure(parent_call_sid=parent_call_sid, reason="playback_not_started")
            return "redirect_failed"
        await call_stages.record_call_stage(parent_call_sid, "vm_playing", {"source": "manual", "drop_id": drop_id})
        return "played"
    except Exception as exc:  # noqa: BLE001 - background task
        logger.error(f"Queued voicemail after greeting failed for {parent_call_sid}: {type(exc).__name__}")
        return "error"


@router.post("/dialer/manual-voicemail-drop")
async def control_manual_voicemail_drop(
    body: ManualVoicemailDropRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user=Depends(require_sales_dialer_role),
):
    """Queue/cancel a server-side Twilio voicemail drop for this rep's own call.

    The browser never gets Twilio credentials or a carrier SID other than its own
    authenticated call. A signed AMD callback gates playback on a confirmed machine
    greeting end; human and uncertain results cannot trigger a voicemail.
    """
    rep_user_id = getattr(user, "provider_id", None)
    if not isinstance(rep_user_id, str) or not rep_user_id:
        raise HTTPException(status_code=503, detail="Your account is missing its dialer user mapping.")

    call_row = await get_dialer_call_child_leg(parent_call_sid=body.parent_call_sid)
    if not call_row or call_row.get("provider") != "twilio" or call_row.get("rep_user_id") != rep_user_id:
        raise HTTPException(status_code=404, detail="This active Twilio call was not found for your account.")
    child_call_sid = call_row.get("child_call_sid")
    if not isinstance(child_call_sid, str) or not _TWILIO_CALL_SID_PATTERN.fullmatch(child_call_sid):
        raise HTTPException(status_code=409, detail="The called party's line is not ready for a voicemail drop.")

    if body.action == "self":
        # "Leave it myself": put the rep on the line with the mailbox the moment it
        # answers, instead of after machine detection (16 s, by which time the
        # greeting and beep are gone and the mailbox records the rep hearing
        # ringing). Already answered and not yet joined: join it now.
        await _remember_leave_self(body.parent_call_sid)
        if call_row.get("status") == "in-progress" and not call_row.get("lead_joined_at"):
            joined = await _join_mailbox_to_rep_now(
                parent_call_sid=body.parent_call_sid, child_call_sid=child_call_sid,
            )
            return {"state": "joined" if joined else "waiting"}
        return {"state": "waiting"}

    if body.action == "cancel":
        result = await request_manual_voicemail_drop(
            parent_call_sid=body.parent_call_sid,
            rep_user_id=rep_user_id,
            cancel=True,
        )
        if not result or result.get("accepted") is not True:
            raise HTTPException(status_code=409, detail="The voicemail has already started and can no longer be cancelled.")
        return {"state": "cancelled"}

    enabled, playback_url, drop_id = await get_auto_voicemail_settings(
        user_id=rep_user_id,
        allow_manual_when_disabled=True,
        parent_call_sid=body.parent_call_sid,
    )
    del enabled  # manual sends work whether automatic drop is on or off
    if not playback_url or not playback_url.split("?", 1)[0].lower().endswith(".wav"):
        raise HTTPException(status_code=409, detail="Record a WAV voicemail before sending one from Twilio.")
    if drop_id:
        await attach_voicemail_drop_to_call(
            parent_call_sid=body.parent_call_sid,
            child_call_sid=child_call_sid,
            drop_id=drop_id,
        )

    result = await request_manual_voicemail_drop(
        parent_call_sid=body.parent_call_sid,
        rep_user_id=rep_user_id,
    )
    if not result:
        raise HTTPException(status_code=503, detail="Could not safely queue this voicemail. The call was left unchanged.")
    if result.get("accepted") is not True:
        reason = result.get("reason")
        detail = {
            "not_voicemail": "Twilio did not confirm that a voicemail greeting had ended, so the message was not sent.",
            "call_ended": "The call has already ended.",
            "already_sent": "A voicemail has already been sent on this call.",
            "not_found": "This active Twilio call was not found for your account.",
        }.get(reason, "The voicemail could not be safely queued.")
        raise HTTPException(status_code=409, detail=detail)

    current = await get_dialer_call_child_leg(parent_call_sid=body.parent_call_sid)
    background_tasks.add_task(
        call_stages.record_call_stage, body.parent_call_sid, "vm_requested",
        {"while": "answered" if (current or {}).get("lead_joined_at") else "ringing"},
    )
    state = _manual_drop_state(current)
    if state == "idle" and result.get("state") in {"waiting", "ready"}:
        # The atomic request RPC is the authoritative write. Preserve its
        # accepted state if a read replica has not observed the timestamp yet.
        state = "waiting"
    if state == "sent" or state == "playing":
        return {"state": state}

    if _manual_drop_ready(current):
        claimed = await claim_manual_voicemail_playback(
            parent_call_sid=body.parent_call_sid,
            child_call_sid=child_call_sid,
        )
        if claimed:
            endpoint_root = str(request.url).split("?", 1)[0].rsplit("/", 1)[0]
            twiml = _voicemail_playback_twiml(
                endpoint_root=endpoint_root,
                parent_call_sid=body.parent_call_sid,
                playback_url=playback_url,
                pause_seconds=_VOICEMAIL_START_PAUSE_SECONDS,
                drop_id=drop_id,
                leg_call_sid=child_call_sid,
            )
            if not await _redirect_twilio_child_to_voicemail(child_call_sid=child_call_sid, twiml=twiml):
                await release_manual_voicemail_playback(
                    parent_call_sid=body.parent_call_sid,
                    child_call_sid=child_call_sid,
                )
                raise HTTPException(status_code=502, detail="Twilio could not start the message. Try again or speak instead.")
            # The prospect leg now plays the message on its own: free the rep.
            background_tasks.add_task(_release_rep_for_voicemail, body.parent_call_sid)
            background_tasks.add_task(call_stages.record_call_stage, body.parent_call_sid, "vm_playing", {})
            return {"state": "playing", "released": True}

    if state == "waiting" and not (current or {}).get("lead_joined_at"):
        # Pressed while it rings or during the greeting (Twilio's AMD is still
        # holding the prospect leg). Hand the line to the server and free the
        # rep now: the signed AMD answer plays the recording on a machine, and
        # a person who answers is hung up rather than put in an empty room
        # (rep_detached_at). Without machine detection nothing would ever
        # confirm the mailbox, so the rep keeps the call in that case.
        policy = await get_team_voicemail_policy(
            user_id=rep_user_id, parent_call_sid=body.parent_call_sid,
        )
        detection_on = (policy or {}).get("machine_detection_enabled") is not False
        # With machine detection off the call sorter is what hears the greeting end
        # (the answer webhook then holds the line and plays on the beep). With neither,
        # nothing could confirm a mailbox and the rep keeps the call.
        server_can_hold = detection_on or call_sorter.call_sorter_enabled()
        if server_can_hold and await mark_rep_detached_for_voicemail(parent_call_sid=body.parent_call_sid):
            background_tasks.add_task(_release_rep_for_voicemail, body.parent_call_sid)
            return {"state": "waiting", "released": True}
    return {"state": state}


@router.get("/dialer/manual-voicemail-drop/{parent_call_sid}")
async def manual_voicemail_drop_status(
    parent_call_sid: str,
    user=Depends(require_sales_dialer_role),
):
    if not _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid):
        raise HTTPException(status_code=400, detail="Invalid call id.")
    rep_user_id = getattr(user, "provider_id", None)
    call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
    if not call_row or call_row.get("provider") != "twilio" or call_row.get("rep_user_id") != rep_user_id:
        raise HTTPException(status_code=404, detail="This Twilio call was not found for your account.")
    child_call_sid = call_row.get("child_call_sid")
    evidence = {}
    if call_row.get("voicemail_drop_started_at") or call_row.get("manual_voicemail_requested_at"):
        evidence = await get_voicemail_drop_evidence(parent_call_sid=parent_call_sid)
    state = _manual_drop_state({**call_row, **{
        key: value for key, value in evidence.items() if key == "voicemail_drop_failed_at"
    }})
    result = {
        "state": state,
        "amd_result": call_row.get("amd_result"),
        "ready": isinstance(child_call_sid, str) and bool(_TWILIO_CALL_SID_PATTERN.fullmatch(child_call_sid)),
    }
    if call_row.get("ended_at"):
        # Lets a rep who already moved on learn that a queued voicemail's line
        # ended without one (no answer, or a person who was hung up on).
        result["ended"] = True
    if state == "failed":
        result["failure"] = evidence.get("voicemail_drop_failure") or "playback_incomplete"
    return call_stages.with_stage(result, await call_stages.latest_call_stage(parent_call_sid))


@router.post("/dialer/telnyx/calls/start", status_code=201)
async def start_telnyx_dialer_call(
    body: TelnyxDialerCallStartRequest,
    user=Depends(require_sales_dialer_role),
):
    """Create a history row before Telnyx originates the PSTN call."""
    assigned = await _get_assigned_dialer_number_or_503(user.id)
    if not assigned or assigned.get("provider") != "telnyx":
        raise HTTPException(status_code=409, detail="This rep has no active Telnyx number assigned.")
    if assigned.get("direction") not in {"outbound", "both"}:
        raise HTTPException(
            status_code=409,
            detail="The assigned Telnyx number is not enabled for outbound calls. Set its direction to Outbound or Inbound & outbound in Dialer → Numbers.",
        )
    if not user.provider_id:
        raise HTTPException(status_code=503, detail="Rep account is missing its Supabase user mapping.")
    block_reason = await phone_block_reason(body.to_number)
    if block_reason:
        raise HTTPException(status_code=403, detail=f"Call blocked by compliance check ({block_reason}).")
    allowed, reason, phone_us_state = await check_dialer_calling_hours_with_state(
        body.to_number, str(body.entry_id) if body.entry_id else None
    )
    if not allowed and rep_overrides_hours(reason, body.hours_override):
        logger.warning(f"Telnyx: rep confirmed the calling-hours warning ({reason}) by {user.provider_id}")
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail=dial_block_message(reason))
    if await call_stages.voicemail_recording_required(str(user.provider_id)):
        raise HTTPException(status_code=409, detail=call_stages.recording_required_detail())
    created = await create_dialer_call(
        parent_call_sid=str(body.call_id),
        rep_user_id=user.provider_id,
        entry_id=str(body.entry_id) if body.entry_id else None,
        from_number=str(assigned["phone_number"]),
        to_number=body.to_number,
        provider="telnyx",
        phone_us_state=phone_us_state,
    )
    if not created:
        raise HTTPException(status_code=503, detail="Could not save this call to dialer history.")
    return {
        "call_id": str(body.call_id),
        "status": "initiated",
        "from_number": str(assigned["phone_number"]),
    }


@router.post("/dialer/telnyx/calls/status", status_code=204)
async def update_telnyx_dialer_call_status(
    body: TelnyxDialerCallStatusRequest,
    user=Depends(require_sales_dialer_role),
):
    if not user.provider_id:
        raise HTTPException(status_code=503, detail="Rep account is missing its Supabase user mapping.")
    await update_dialer_call_status(
        parent_call_sid=str(body.call_id),
        child_call_sid=None,
        # Supabase dialer_calls.status only accepts in-progress for a live call.
        # Telnyx reports the WebRTC leg as "answered"; normalize before storing.
        status="in-progress" if body.status == "answered" else body.status,
        duration_seconds=body.duration_seconds,
        rep_user_id=user.provider_id,
        provider="telnyx",
    )
    return None


@router.post("/dialer/telnyx/inbound/start", status_code=201)
async def start_telnyx_inbound_dialer_call(
    body: TelnyxInboundCallRequest,
    user=Depends(require_sales_dialer_role),
):
    """Record an inbound WebRTC invite only on the rep assigned to that DID."""
    try:
        owns_number = await user_owns_dialer_number(user.id, body.to_number, "telnyx")
    except DialerNumberAssignmentUnavailable as exc:
        raise HTTPException(status_code=503, detail="Could not verify the incoming Telnyx line.") from exc
    if not owns_number:
        raise HTTPException(status_code=404, detail="This incoming Telnyx call is not assigned to this rep.")
    if not user.provider_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Inbound Telnyx call logging is not configured.")

    inbound = {
        "provider_call_id": body.call_id,
        "provider": "telnyx",
        "from_number": body.from_number,
        "to_number": body.to_number,
        "conference_name": f"telnyx-direct-{body.call_id}",
        "status": "ringing",
        "target_user_ids": [user.provider_id],
        "caller_name": body.caller_name or None,
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL.rstrip('/')}/rest/v1/inbound_calls?on_conflict=provider_call_id",
                json=inbound,
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json",
                    # Duplicate SDK notifications or multiple sessions for one rep must
                    # never reset an already-answered call to ringing.
                    "Prefer": "resolution=ignore-duplicates,return=representation",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                existing = await client.get(
                    f"{SUPABASE_URL.rstrip('/')}/rest/v1/inbound_calls",
                    params={
                        "select": "id",
                        "provider_call_id": f"eq.{body.call_id}",
                        "provider": "eq.telnyx",
                        "target_user_ids": f"cs.{{{user.provider_id}}}",
                        "limit": "1",
                    },
                    headers={
                        "apikey": SUPABASE_ANON_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    },
                    timeout=5.0,
                )
                existing.raise_for_status()
                rows = existing.json()
    except Exception as exc:  # noqa: BLE001 - call signaling must remain independent of logging
        logger.error(f"Could not record Telnyx inbound call {body.call_id}: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="Could not prepare the inbound call in the dialer.") from exc
    if not rows or not isinstance(rows[0], dict):
        raise HTTPException(status_code=503, detail="Telnyx inbound call record was not created.")
    return {"id": rows[0]["id"], "provider_call_id": body.call_id}


@router.post("/dialer/telnyx/inbound/answer", status_code=204)
async def answer_telnyx_inbound_dialer_call(
    body: TelnyxInboundCallAnswerRequest,
    user=Depends(require_sales_dialer_role),
):
    """Atomically mark this rep's ringing Telnyx invite answered."""
    if not user.provider_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Inbound Telnyx call logging is not configured.")
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/inbound_calls"
    scope = {
        "provider_call_id": f"eq.{body.call_id}",
        "provider": "eq.telnyx",
        "target_user_ids": f"cs.{{{user.provider_id}}}",
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                base,
                params={**scope, "status": "eq.ringing"},
                json={"status": "answered", "answered_by": user.provider_id},
                headers=headers,
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
            if rows:
                return None
            existing = await client.get(
                base,
                params={**scope, "select": "status,answered_by", "limit": "1"},
                headers=headers,
                timeout=5.0,
            )
            existing.raise_for_status()
            records = existing.json()
    except Exception as exc:  # noqa: BLE001 - signaling ownership must fail closed
        logger.error(f"Could not claim Telnyx inbound call {body.call_id}: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="Could not verify this inbound call.") from exc
    if not records:
        raise HTTPException(status_code=404, detail="This inbound call is not assigned to this rep.")
    row = records[0]
    if row.get("status") == "answered" and row.get("answered_by") == user.provider_id:
        return None
    raise HTTPException(status_code=409, detail="This inbound call has already been answered or ended.")


@router.post("/dialer/telnyx/inbound/end", status_code=204)
async def end_telnyx_inbound_dialer_call(
    body: TelnyxInboundCallEndRequest,
    user=Depends(require_sales_dialer_role),
):
    if not user.provider_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Inbound Telnyx call logging is not configured.")
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    now = datetime.now(UTC).isoformat()
    ended_status = "failed" if body.outcome == "failed" else "missed"
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/inbound_calls"
    params = {
        "provider_call_id": f"eq.{body.call_id}",
        "provider": "eq.telnyx",
        "target_user_ids": f"cs.{{{user.provider_id}}}",
    }
    try:
        async with httpx.AsyncClient() as client:
            # An unanswered invite becomes missed. If another request has already
            # claimed it, the following patch preserves that answered status.
            missed = await client.patch(
                base,
                params={**params, "status": "eq.ringing"},
                json={"status": ended_status, "ended_at": now},
                headers=headers,
                timeout=5.0,
            )
            missed.raise_for_status()
            answered = await client.patch(
                base,
                params={**params, "status": "eq.answered"},
                json={
                    "status": "failed" if body.outcome == "failed" else "answered",
                    "ended_at": now,
                },
                headers=headers,
                timeout=5.0,
            )
            answered.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - never block teardown on a history write
        logger.error(f"Could not close Telnyx inbound call {body.call_id}: {type(exc).__name__}")
    await update_dialer_call_status(
        parent_call_sid=body.call_id,
        child_call_sid=None,
        status={"completed": "completed", "failed": "failed", "missed": "no-answer"}[body.outcome],
        duration_seconds=body.duration_seconds if body.outcome == "completed" else None,
        rep_user_id=user.provider_id,
        provider="telnyx",
    )
    return None


async def _resolve_dialer_auth_token() -> str | None:
    """Auth token for the independent sales dialer Twilio account."""
    try:
        return await db_client.get_platform_dialer_auth_token()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Dialer auth token lookup failed: {exc}")
        return None


async def _resolve_dialer_caller_id() -> str | None:
    """Default caller ID from the independent sales dialer Twilio record."""
    try:
        creds = await db_client.get_platform_dialer_credentials()
        return creds.get("default_caller_id") if creds else None
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Dialer caller-id lookup failed: {exc}")
        return None


async def _verify_twilio_signature(request: Request, form_data: dict) -> bool:
    auth_token = await _resolve_dialer_auth_token()
    signature = request.headers.get("x-twilio-signature", "")
    if not auth_token or not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(str(request.url), form_data, signature)


async def _live_transcription_twiml(
    *, backend_endpoint: str, channel_key: str, leg_call_sid: str
) -> str:
    """The rep-leg <Start><Stream> for live captions, or "" -- never raises.

    Optional on every path: a missing key, endpoint or auth token costs the captions
    and nothing else, so it can never turn into a failed call.
    """
    try:
        if not twilio_live_transcribe.live_transcription_enabled():
            return ""
        secret = await _resolve_dialer_auth_token()
        return twilio_live_transcribe.stream_twiml(
            backend_endpoint=str(backend_endpoint or ""),
            channel_key=channel_key,
            leg_call_sid=leg_call_sid,
            secret=secret or "",
        )
    except Exception as exc:  # noqa: BLE001 - captions must never cost the call
        logger.warning(f"Live transcription stream omitted: {type(exc).__name__}")
        return ""


@router.websocket("/dialer-twilio-transcribe")
async def handle_dialer_twilio_transcribe(websocket: WebSocket):
    """Twilio Media Stream from the rep's leg -> attributed live captions (contract C1)."""
    async def secret() -> str | None:
        return await _resolve_dialer_auth_token()

    await twilio_live_transcribe.serve_twilio_transcription(websocket, secret)


@router.websocket("/dialer-call-sorter")
async def handle_dialer_call_sorter(websocket: WebSocket):
    """Twilio Media Stream from the PROSPECT leg -> the call sorter (F5)."""
    async def secret() -> str | None:
        return await _resolve_dialer_auth_token()

    await dialer_call_sorter.serve_twilio_sorter(websocket, secret)


async def _start_call_sorter_stream(*, call_ref: str, leg_call_sid: str) -> None:
    """Fork an answered prospect leg to the call sorter. Background task, never raises.

    Started from the leg's `in-progress` status callback: synchronous AMD holds the
    join TwiML until the greeting has ended, which is too late to listen to it.
    """
    try:
        backend_endpoint, _ = await get_backend_endpoints()
        await dialer_call_sorter.start_prospect_stream(
            await _twilio_client(),
            backend_endpoint=str(backend_endpoint or ""),
            call_ref=call_ref,
            leg_call_sid=leg_call_sid,
            secret=await _resolve_dialer_auth_token() or "",
        )
    except Exception as exc:  # noqa: BLE001 - listening is optional; the call is not
        logger.warning(f"Call sorter stream not started for {call_ref}: {type(exc).__name__}")


_LEAVE_SELF_TTL_SECONDS = 15 * 60


def _leave_self_key(parent_call_sid: str) -> str:
    return f"dialer:leave_self:{parent_call_sid}"


async def _remember_leave_self(parent_call_sid: str) -> None:
    """Shared across workers: the status callback may land on another one. Never raises."""
    try:
        client = call_sorter._redis_client()
        try:
            await client.set(_leave_self_key(parent_call_sid), "1", ex=_LEAVE_SELF_TTL_SECONDS)
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not remember leave-it-myself for {parent_call_sid}: {exc}")


async def _leave_self_requested(parent_call_sid: str) -> bool:
    try:
        client = call_sorter._redis_client()
        try:
            return bool(await client.get(_leave_self_key(parent_call_sid)))
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 - unknown means the usual AMD path
        return False


async def _join_mailbox_to_rep_now(*, parent_call_sid: str, child_call_sid: str) -> bool:
    """Send the answered lead leg straight into the rep's conference (ending the
    AMD hold), so the rep hears the greeting and the beep and speaks after it."""
    client = await _twilio_client()
    if not client:
        return False
    backend_endpoint, _ = await get_backend_endpoints()
    root = f"{str(backend_endpoint or '').rstrip('/')}/api/v1/telephony"
    join_url = f"{root}/dialer-conference-join?" + urlencode({
        "conference_name": conference_name_for(parent_call_sid),
        "muted": "false",
        "end_on_exit": "true",
        "start_on_enter": "true",
        "events_url": f"{root}/dialer-conference-events",
        "recording_url": f"{root}/dialer-recording-callback",
        "parent_call_sid": parent_call_sid,
        "handled": "self",
    })
    try:
        await asyncio.to_thread(client.calls(child_call_sid).update, url=join_url, method="POST")
    except Exception as exc:  # noqa: BLE001 - the AMD path still joins it later
        logger.warning(f"Could not join the mailbox to the rep on {parent_call_sid}: {exc}")
        return False
    logger.info(f"leave-it-myself: joined {child_call_sid} to the rep on {parent_call_sid}")
    return True


# How long after the answer the beep watcher keeps listening: past AMD's longest
# limit (30 s on "accurate") plus a long greeting.
_BEEP_WATCH_SECONDS = 45.0


async def _beep_trial_watch(*, parent_call_sid: str, child_call_sid: str) -> str:
    """Beep-timed voicemail (trial reps only, see call_sorter.beep_trial_user).

    Twilio's DetectMessageEnd almost never hears the beep here (48 of 48 mailboxes
    in 14 days ran to its time limit), so the recording started at a fixed moment:
    over long greetings, or after silence long enough for the mailbox to hang up.
    The sorter listens to the answered leg; on the beep, a voicemail that should
    play (a queued Send voicemail, or the rep's automatic mode) starts at once.
    Anything else (a person, ask mode with no request, a mailbox already handled)
    leaves the call to the usual AMD path. Background task, never raises.
    """
    try:
        row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        rep_user_id = (row or {}).get("rep_user_id")
        if not row or row.get("provider") != "twilio" or not call_sorter.beep_trial_user(rep_user_id):
            return "not_trial"
        if not call_sorter.call_sorter_enabled():
            backend_endpoint, _ = await get_backend_endpoints()
            started = await dialer_call_sorter.start_prospect_stream(
                await _twilio_client(),
                backend_endpoint=str(backend_endpoint or ""),
                call_ref=parent_call_sid,
                leg_call_sid=child_call_sid,
                secret=await _resolve_dialer_auth_token() or "",
                force=True,
            )
            if not started:
                return "no_stream"
        state = await call_sorter.wait_for_sorter_stage(
            parent_call_sid, stages=("beep", "mailbox_full", "person"), timeout=_BEEP_WATCH_SECONDS,
        )
        stage = (state or {}).get("stage")
        if stage != "beep":
            return stage or "no_beep"
        row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        if (
            not row or row.get("ended_at") or row.get("voicemail_drop_started_at")
            or row.get("auto_voicemail_sent") or row.get("child_call_sid") != child_call_sid
        ):
            return "already_handled"
        manual = bool(row.get("manual_voicemail_requested_at"))
        enabled, playback_url, drop_id = await get_auto_voicemail_settings(
            user_id=rep_user_id, allow_manual_when_disabled=manual, parent_call_sid=parent_call_sid,
        )
        policy = await get_team_voicemail_policy(user_id=rep_user_id, parent_call_sid=parent_call_sid)
        on_machine = str((policy or {}).get("on_machine") or "drop_vm")
        # Automatic only while the rep is not on the line with the mailbox (ask/self
        # modes hand it to them; they decide). A queued Send voicemail always plays.
        should_play = manual or (enabled and on_machine == "drop_vm" and not row.get("lead_joined_at"))
        if not should_play:
            return "not_requested"
        if not playback_url or not playback_url.split("?", 1)[0].lower().endswith(".wav"):
            return "no_recording"
        # The beep stands in for AMD's verdict so the one-shot claim applies.
        await update_dialer_call_amd_result(parent_call_sid=parent_call_sid, answered_by="machine_end_beep")
        if not await claim_manual_voicemail_playback(
            parent_call_sid=parent_call_sid, child_call_sid=child_call_sid, manual=manual,
        ):
            return "not_claimed"
        if drop_id:
            await attach_voicemail_drop_to_call(
                parent_call_sid=parent_call_sid, child_call_sid=child_call_sid, drop_id=drop_id,
            )
        backend_endpoint, _ = await get_backend_endpoints()
        twiml = _voicemail_playback_twiml(
            endpoint_root=f"{str(backend_endpoint or '').rstrip('/')}/api/v1/telephony",
            parent_call_sid=parent_call_sid,
            playback_url=playback_url,
            pause_seconds=0,
            drop_id=drop_id,
            leg_call_sid=child_call_sid,
        )
        if not await _redirect_twilio_child_to_voicemail(child_call_sid=child_call_sid, twiml=twiml):
            await release_manual_voicemail_playback(
                parent_call_sid=parent_call_sid, child_call_sid=child_call_sid,
            )
            return "redirect_failed"
        logger.info(f"beep trial: played the voicemail on the beep for {parent_call_sid}")
        await call_stages.record_call_stage(
            parent_call_sid, "vm_playing", {"source": "beep", "manual": manual, "drop_id": drop_id},
        )
        if not manual:
            await _release_rep_for_voicemail(parent_call_sid)
        return "played"
    except Exception as exc:  # noqa: BLE001 - a trial must never break the call
        logger.warning(f"beep trial failed for {parent_call_sid}: {type(exc).__name__}: {exc}")
        return "error"


def _maybe_start_call_sorter(
    background_tasks: BackgroundTasks, *, call_ref: str, leg_call_sid: str | None,
) -> None:
    if leg_call_sid and call_ref and call_sorter.call_sorter_enabled():
        background_tasks.add_task(_start_call_sorter_stream, call_ref=call_ref, leg_call_sid=leg_call_sid)


@router.post("/voice-connect", include_in_schema=False)
async def handle_voice_connect(request: Request, background_tasks: BackgroundTasks):
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on voice-connect webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    # Listen-in arrives HERE, not at /dialer-listen-connect, and this branch
    # is the only reason the feature works at all.
    #
    # generate_voice_access_token grants a VoiceGrant over one
    # TWILIO_TWIML_APP_SID, a TwiML App has exactly one Voice URL, and that
    # URL is this endpoint. device.connect() can pass arbitrary custom params
    # but cannot pick a different URL - so a manager's listen-in leg lands on
    # voice-connect with a ListenParentCallSid and no To, and would otherwise
    # fall straight through to the "missing To number" hangup below.
    #
    # Routing only, never an authorization shortcut: _serve_listen_in_twiml
    # runs its own full is_manager_or_admin check, so a sales_rep who adds
    # this param to their own dial is rejected there. The signature has
    # already been verified once, above, and the parsed form is handed over
    # rather than re-read - the body is consumed and cannot be read twice.
    #
    # Placed before every other field read so the rep dial path - the hot
    # path on every sales call - is untouched when the param is absent: one
    # dict lookup against a falsy default.
    if str(form_data.get("ListenParentCallSid", "")).strip():
        return await _serve_listen_in_twiml(form_data)

    # A claimed inbound call (including a parallel second-pickup handoff) is
    # already a PSTN leg waiting in a conference. The browser originates only
    # its own WebRTC leg; dialing the conference name as a phone number would
    # fail or, worse, fall through to the ordinary outbound call path.
    inbound_conference = str(
        form_data.get("conference") or form_data.get("Conference") or ""
    ).strip()
    if inbound_conference:
        raw_from = str(form_data.get("From") or "")
        rep_id = _parse_rep_id_from_identity(raw_from)
        parent_call_sid = parent_call_sid_from_conference_name(inbound_conference)
        if (
            not rep_id
            or not parent_call_sid
            or not _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid)
            or inbound_conference != conference_name_for(parent_call_sid)
        ):
            logger.warning("Rejected malformed Twilio inbound conference join")
            return HTMLResponse(content=hangup, media_type="application/xml")
        rep = await db_client.get_user_by_id(rep_id)
        rep_user_id = str(getattr(rep, "provider_id", "") or "")
        if not rep_user_id or not await is_claimed_inbound_conference(
            conference_name=inbound_conference, rep_user_id=rep_user_id
        ):
            logger.warning("Rejected Twilio conference join without an inbound claim for this rep")
            return HTMLResponse(content=hangup, media_type="application/xml")
        rep_call_sid = str(form_data.get("CallSid") or "")
        if not _TWILIO_CALL_SID_PATTERN.fullmatch(rep_call_sid) or not await bind_inbound_rep_leg(
            conference_name=inbound_conference, rep_user_id=rep_user_id,
            provider="twilio", rep_call_id=rep_call_sid,
        ):
            logger.warning("Rejected an unbound/replaced Twilio inbound rep leg")
            return HTMLResponse(content=hangup, media_type="application/xml")
        # A ring-back after a voicemail hand-off: the rep is back with the person.
        background_tasks.add_task(call_stages.record_ringback_joined, inbound_conference)
        # An extra answer: a teammate took it, or the agent's transfer-back target is
        # in the room and the agent can hand over now.
        background_tasks.add_task(
            dialer_extra_answer.on_rep_joined, conference_name=inbound_conference, rep_user_id=rep_user_id,
        )
        try:
            backend_endpoint, _ = await get_backend_endpoints()
        except Exception as exc:  # noqa: BLE001 - keep the call but omit optional telemetry
            logger.warning(f"Could not resolve inbound conference callback URLs: {exc}")
            backend_endpoint = ""
        endpoint = str(backend_endpoint or "").rstrip("/")
        telemetry_attributes = _conference_telemetry_attributes(
            events_url=f"{endpoint}/api/v1/telephony/dialer-conference-events",
            recording_url=f"{endpoint}/api/v1/telephony/dialer-recording-callback",
        )
        # Live captions for the rep's coach, attributed by track. Keyed by the
        # caller's call SID (the conference's own name), which is what /sw-listen
        # resolves an inbound_calls row to. "" when not configured.
        live_stream = await _live_transcription_twiml(
            backend_endpoint=endpoint, channel_key=parent_call_sid, leg_call_sid=rep_call_sid,
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            + live_stream
            + "<Say>This call may be recorded and monitored for quality assurance.</Say>"
            "<Dial><Conference "
            + telemetry_attributes
            + 'startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">'
            + escape(inbound_conference)
            + "</Conference></Dial></Response>"
        )
        return HTMLResponse(content=twiml, media_type="application/xml")

    to_number = form_data.get("To", "").strip()
    raw_from = form_data.get("From", "")
    entry_id = form_data.get("EntryId", "").strip() or None
    double_dial_authorization_id = str(form_data.get("DoubleDialAuthorization") or "").strip()
    parent_call_sid = form_data.get("CallSid", "")
    parallel_requested = str(form_data.get("DialMode", "")).strip().lower() == "parallel"
    parallel_entry_ids: list[str] = []
    phone_us_state: str | None = None
    parallel_line_count = 0
    if parallel_requested:
        try:
            raw_entry_ids = json.loads(str(form_data.get("ParallelEntryIds", "")))
            parallel_line_count = int(str(form_data.get("ParallelLineCount", "")))
            if parallel_line_count not in {2, 3, 5, 10} or not isinstance(raw_entry_ids, list):
                raise ValueError("invalid parallel request")
            parallel_entry_ids = [str(UUID(str(value))) for value in raw_entry_ids]
            if len(parallel_entry_ids) != parallel_line_count or len(set(parallel_entry_ids)) != parallel_line_count:
                raise ValueError("parallel entries do not match the selected line count")
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("voice-connect rejected malformed parallel dial request")
            return HTMLResponse(content=hangup, media_type="application/xml")
        # Adaptive line count (Phase 1b): a list that answers a lot dials fewer lines,
        # so fewer people answer at once with nobody free to take them.
        capped_entry_ids = extra_answer.trim_to_line_cap(
            parallel_entry_ids, await extra_answer.parallel_line_cap(parallel_entry_ids),
        )
        if len(capped_entry_ids) != parallel_line_count:
            logger.info(f"voice-connect trimmed a parallel batch to {len(capped_entry_ids)} lines (line cap)")
            parallel_entry_ids = capped_entry_ids
            parallel_line_count = len(capped_entry_ids)
    if double_dial_authorization_id and (parallel_requested or not entry_id):
        logger.warning("voice-connect rejected double-dial authorization without one queue entry")
        return HTMLResponse(content=hangup, media_type="application/xml")
    # Three-tier resolution, most specific first:
    #   1. the number assigned to THIS rep (dialer_phone_numbers), then
    #   2. the active platform Twilio account's default_caller_id from the
    #      platform_twilio_credentials table, then
    #   3. the SYSEVO_TWILIO_DEFAULT_CALLER_ID env var.
    # Tiers 2 and 3 are both inside _resolve_dialer_caller_id, which is what
    # makes the per-account default apply here as well as to signature
    # verification; tier 1 stays in front of it so per-rep assignment is not
    # lost when an account-level default exists (it always does in prod).
    try:
        assigned_caller_id = await resolve_assigned_caller_id(raw_from)
    except (DialerNumberProviderMismatch, DialerNumberAssignmentUnavailable) as exc:
        logger.warning(f"Twilio dial rejected for {raw_from!r}: {exc}")
        assigned_caller_id = ""
        provider_mismatch = True
    else:
        provider_mismatch = False
    caller_id = assigned_caller_id or (
        "" if provider_mismatch else (await _resolve_dialer_caller_id() or "")
    )
    if (not parallel_requested and not to_number) or not caller_id or not parent_call_sid:
        logger.error(
            "voice-connect missing To number, a resolvable default caller id, or CallSid"
        )
        return HTMLResponse(
            content=_parallel_refusal_twiml(
                "No caller ID number is set up for this line, so the call was not placed. Ask your admin to assign one."
            ),
            media_type="application/xml",
        )

    if parallel_requested:
        candidate_numbers = await dialer_parallel.candidate_phones(parallel_entry_ids)
        if len(candidate_numbers) != parallel_line_count:
            logger.warning("voice-connect could not load every parallel destination for compliance preflight")
            return HTMLResponse(
                content=_parallel_refusal_twiml("The selected leads could not be loaded. Refresh the queue and try again."),
                media_type="application/xml",
            )
        # Every destination gets the same server checks as a single dial: the
        # do-not-call register and calling hours. A lead that fails either is dropped
        # and the rest dial (never the whole batch refused); the batch snaps down to a
        # supported size. Checks run concurrently to stay inside Twilio's deadline.
        candidate_items = list(candidate_numbers.items())
        block_reasons, hours_results = await asyncio.gather(
            asyncio.gather(*(
                get_dialer_phone_block_reason(candidate_phone)
                for _candidate_entry_id, candidate_phone in candidate_items
            )),
            asyncio.gather(*(
                check_dialer_calling_hours(candidate_phone, candidate_entry_id)
                for candidate_entry_id, candidate_phone in candidate_items
            )),
        )
        keep_ids: list[str] = []
        skipped: dict[str, str] = {}
        for (candidate_entry_id, _phone), blocked, (hours_ok, hours_reason) in zip(
            candidate_items, block_reasons, hours_results
        ):
            if blocked or not hours_ok:
                skipped[candidate_entry_id] = str(blocked or hours_reason or "compliance_unavailable")
            else:
                keep_ids.append(candidate_entry_id)
        if skipped:
            lines = snap_parallel_lines(len(keep_ids))
            logger.warning(
                f"voice-connect skipped {len(skipped)} parallel lead(s) "
                f"({sorted(set(skipped.values()))}); dialing {lines} of {parallel_line_count}"
            )
            if lines < 2:
                return HTMLResponse(
                    content=_parallel_refusal_twiml(BATCH_TOO_SMALL_MESSAGE),
                    media_type="application/xml",
                )
            kept = set(keep_ids)
            parallel_entry_ids = [entry for entry in parallel_entry_ids if entry in kept][:lines]
            parallel_line_count = lines
    else:
        block_reason = await get_dialer_phone_block_reason(to_number)
        if block_reason:
            logger.warning(
                f"voice-connect blocked outbound dial by server compliance check ({block_reason})"
            )
            return HTMLResponse(content=_dial_block_twiml(block_reason), media_type="application/xml")
        allowed, reason, phone_us_state = await check_dialer_calling_hours_with_state(
            to_number, entry_id
        )
        if not allowed and rep_overrides_hours(reason, form_data.get("HoursOverride")):
            logger.warning(
                f"voice-connect: rep chose Call anyway past the calling-hours check ({reason}) "
                f"for {parent_call_sid} by {raw_from}"
            )
            allowed = True
        if not allowed:
            logger.warning(
                f"voice-connect blocked outbound dial by calling-hours check ({reason})"
            )
            return HTMLResponse(content=_dial_block_twiml(reason), media_type="application/xml")

    rep_id = _parse_rep_id_from_identity(raw_from)
    team_voicemail_policy: dict[str, Any] | None = None
    rep_user_id: str | None = None
    parallel_targets: list[dict] = []
    if rep_id is not None:
        user = await db_client.get_user_by_id(rep_id)
        if user and user.provider_id:
            rep_user_id = str(user.provider_id)
            if await call_stages.voicemail_recording_required(rep_user_id):
                # Recording lock (reps only): the browser blocks this too; the
                # database refuses the call row as the last line.
                logger.warning(f"voice-connect refused {parent_call_sid}: {call_stages.RECORDING_REQUIRED_CODE}")
                return HTMLResponse(
                    content=_parallel_refusal_twiml(call_stages.RECORDING_REQUIRED_MESSAGE),
                    media_type="application/xml",
                )
            double_dial_retry_of: str | None = None
            if double_dial_authorization_id:
                try:
                    authorization_uuid = str(UUID(double_dial_authorization_id))
                except (ValueError, TypeError):
                    logger.warning("voice-connect rejected malformed double-dial authorization id")
                    return HTMLResponse(content=hangup, media_type="application/xml")
                consumed_authorization = await consume_double_dial_authorization(
                    authorization_id=authorization_uuid,
                    rep_user_id=rep_user_id,
                    entry_id=entry_id or "",
                    to_number=to_number,
                    call_sid=parent_call_sid,
                )
                if not consumed_authorization:
                    logger.warning("voice-connect rejected expired, used or mismatched double-dial authorization")
                    return HTMLResponse(content=hangup, media_type="application/xml")
                # Pin the exact caller ID that rang on the original attempt.
                caller_id = consumed_authorization["from_number"]
                double_dial_retry_of = consumed_authorization["retry_of_call_id"]
            if parallel_requested:
                created = await dialer_parallel.start_parallel_session(
                    parent_call_sid=parent_call_sid,
                    rep_user_id=rep_user_id,
                    entry_ids=parallel_entry_ids,
                    line_count=parallel_line_count,
                )
                if not created:
                    logger.warning(f"voice-connect could not validate parallel session for {parent_call_sid}")
                    return HTMLResponse(
                        content=_parallel_refusal_twiml(
                            "The parallel batch could not start because one of the leads is no longer available. Refresh the queue and try again."
                        ),
                        media_type="application/xml",
                    )
                parallel_targets = await dialer_parallel.parallel_targets(
                    parent_call_sid=parent_call_sid,
                    rep_user_id=rep_user_id,
                )
                if len(parallel_targets) != parallel_line_count:
                    logger.error(f"voice-connect loaded incomplete parallel targets for {parent_call_sid}")
                    await dialer_parallel.finish_parallel_session(
                        parent_call_sid=parent_call_sid, status="failed"
                    )
                    return HTMLResponse(content=hangup, media_type="application/xml")
                candidate_caller_ids = await parallel_caller_id_candidates(rep_id, caller_id)
                caller_ids_by_entry = choose_parallel_caller_ids(
                    parallel_targets, candidate_caller_ids, caller_id
                )
                if len(caller_ids_by_entry) != parallel_line_count:
                    await dialer_parallel.finish_parallel_session(
                        parent_call_sid=parent_call_sid, status="failed"
                    )
                    return HTMLResponse(content=hangup, media_type="application/xml")
                # Server-resolved entry data, not the custom Voice SDK To value,
                # controls the aggregate row and all outbound PSTN destinations.
                to_number = parallel_targets[0]["phone"]
                entry_id = parallel_targets[0]["entry_id"]
                caller_id = caller_ids_by_entry[entry_id]
            # The aggregate history row must exist BEFORE any per-leg caller ID
            # is persisted: dialer_parallel_assign_caller_id only accepts a
            # caller ID that equals this row's from_number or is a DID assigned
            # to the rep, and it locates the row by parent_call_sid. Persisting
            # first (the original order) matched zero rows, so every Twilio
            # parallel batch was failed before a single destination was rung.
            call_row_created = await create_dialer_call(
                parent_call_sid=parent_call_sid,
                rep_user_id=user.provider_id,
                entry_id=entry_id,
                from_number=caller_id,
                to_number=to_number,
                phone_us_state=phone_us_state,
                double_dial_retry_of=double_dial_retry_of,
                double_dial_authorization_id=authorization_uuid if double_dial_authorization_id else None,
            )
            if not call_row_created:
                logger.error(
                    f"voice-connect could not reserve dialer call {parent_call_sid}; refusing PSTN origination"
                )
                if parallel_requested:
                    await dialer_parallel.finish_parallel_session(
                        parent_call_sid=parent_call_sid, status="failed"
                    )
                return HTMLResponse(
                    content='<?xml version="1.0" encoding="UTF-8"?><Response><Say>We could not verify this call. Please try again.</Say><Hangup/></Response>',
                    media_type="application/xml",
                )
            if parallel_requested:
                for target in parallel_targets:
                    selected_caller_id = caller_ids_by_entry[target["entry_id"]]
                    persisted = await dialer_parallel.assign_parallel_caller_id(
                        parent_call_sid=parent_call_sid,
                        leg_id=target["leg_id"],
                        rep_user_id=rep_user_id,
                        caller_id=selected_caller_id,
                    )
                    if not persisted:
                        logger.error(
                            f"voice-connect could not persist the caller ID for parallel leg {target['leg_id']}"
                        )
                        await dialer_parallel.finish_parallel_session(
                            parent_call_sid=parent_call_sid, status="failed"
                        )
                        await update_dialer_call_status(
                            parent_call_sid=parent_call_sid,
                            child_call_sid=None,
                            status="failed",
                            duration_seconds=0,
                        )
                        return HTMLResponse(
                            content=_parallel_refusal_twiml(
                                "The parallel batch could not start because a caller ID could not be confirmed. Please try again."
                            ),
                            media_type="application/xml",
                        )
                    target["caller_id"] = selected_caller_id
            team_voicemail_policy = await get_team_voicemail_policy(
                user_id=user.provider_id, parent_call_sid=parent_call_sid,
                entry_id=entry_id,
            )
    if parallel_requested and (rep_user_id is None or not parallel_targets):
        logger.warning(f"voice-connect rejected parallel request from an unresolved rep identity")
        return HTMLResponse(content=hangup, media_type="application/xml")
    if rep_user_id is None:
        # An outbound dial whose identity does not resolve to a dialer user skips the
        # recording lock, the call-history row and the per-rep compliance checks. It is
        # refused rather than placed. (Inbound answers returned above.)
        logger.warning(f"voice-connect refused {parent_call_sid}: the caller identity is not a dialer user")
        return HTMLResponse(
            content=_parallel_refusal_twiml("This call could not be verified. Sign in to the dialer again and retry."),
            media_type="application/xml",
        )

    # Reuse the same publicly-reachable backend URL resolution every other
    # telephony provider webhook in this codebase uses (env var, falling
    # back to a cloudflared tunnel URL for local dev) rather than adding a
    # second, parallel "public base URL" concept.
    #
    # This is caught rather than propagated so the failure is a clean,
    # explained hangup instead of a 500 - but it IS fatal to the call now.
    # Under the old <Dial><Number> shape an empty endpoint only cost us
    # status/recording callbacks; with Conference the lead's join URL is
    # handed to Twilio's REST API, which rejects a non-absolute `url`, so
    # calls.create raises, dial_lead_into_conference returns None, and the
    # rep gets the "could not connect" message below.
    try:
        backend_endpoint, _ = await get_backend_endpoints()
    except Exception as exc:  # noqa: BLE001 - deliberate: must never break call setup
        logger.error(f"voice-connect could not resolve backend endpoint for callbacks: {exc}")
        backend_endpoint = ""

    if parallel_requested and not backend_endpoint:
        await dialer_parallel.finish_parallel_session(
            parent_call_sid=parent_call_sid, status="failed"
        )
        await update_dialer_call_status(
            parent_call_sid=parent_call_sid,
            child_call_sid=None,
            status="failed",
            duration_seconds=0,
        )
        return HTMLResponse(content=hangup, media_type="application/xml")

    conference_name = conference_name_for(parent_call_sid)
    conference_events_url = f"{backend_endpoint}/api/v1/telephony/dialer-conference-events"
    recording_callback_url = f"{backend_endpoint}/api/v1/telephony/dialer-recording-callback"
    # Plain URL query string, passed to Twilio's REST API as a function
    # argument rather than embedded in XML - the "&" separators are correct
    # as-is here and must NOT be XML-escaped.
    #
    # end_on_exit=true is load-bearing: the lead hanging up first is the
    # majority outcome on a cold-call dialer, and it must tear the whole
    # conference down so the rep's leg ends too. That's what fires the Voice
    # SDK's `disconnect` event, which is what makes the frontend show the
    # disposition panel. With end_on_exit=false the rep would be left alone
    # in a live conference, in silence, with a running timer and no prompt.
    #
    # events_url/recording_url are handed to the lead's leg so it can repeat
    # the conference-level attributes verbatim - see
    # _conference_telemetry_attributes on why every leg needs them. They're
    # passed rather than re-resolved in that handler for two reasons: the
    # lead is on the line waiting for that TwiML, and get_backend_endpoints()
    # can block on a cloudflared lookup; and resolving once here is what
    # makes the two legs' attributes identical by construction instead of by
    # coincidence. Query params are covered by Twilio's signature (validated
    # against the full request URL), and this endpoint already trusts a far
    # more sensitive signed param in conference_name - which decides which
    # live call you are placed into.
    #
    # urlencode, not concatenation: these values are absolute URLs whose "/"
    # and ":" would otherwise land raw in a query string.
    common_join_fields = {
        "conference_name": conference_name,
        "muted": "false",
        "end_on_exit": "true",
        "start_on_enter": "true",
        "events_url": conference_events_url,
        "recording_url": recording_callback_url,
        "parent_call_sid": parent_call_sid,
    }
    status_callback_base = f"{backend_endpoint}/api/v1/telephony/dialer-call-status"

    if parallel_requested:
        async def originate_parallel_target(target: dict) -> str | None:
            leg_id = target["leg_id"]
            target_entry_id = target["entry_id"]
            join_url = (
                f"{backend_endpoint}/api/v1/telephony/dialer-conference-join?"
                + urlencode({
                    **common_join_fields,
                    "parallel_leg_id": leg_id,
                    "parallel_entry_id": target_entry_id,
                })
            )
            status_url = status_callback_base + "?" + urlencode({
                "parent_call_sid": parent_call_sid,
                "parallel_leg_id": leg_id,
            })
            sid = await dial_lead_into_conference(
                parent_call_sid=parent_call_sid,
                lead_number=target["phone"],
                caller_id=target["caller_id"],
                join_conference_url=join_url,
                status_callback_url=status_url,
                ringing_timeout_seconds=30,
                # Parallel dialing must screen every line before any lead is
                # allowed into the conference, even when auto-drop is off.
                machine_detection=(
                    True if parallel_requested else
                    (team_voicemail_policy or {}).get("machine_detection_enabled") is not False
                ),
                machine_detection_preset=str((team_voicemail_policy or {}).get("machine_detection_preset") or "balanced"),
            )
            if not sid:
                await dialer_parallel.update_parallel_leg(
                    child_call_sid=None, leg_id=leg_id, status="failed"
                )
                return None
            if not await dialer_parallel.register_parallel_leg(leg_id=leg_id, child_call_sid=sid):
                # A faster answered leg may already have won while this
                # originate request was returning. Do not leave this call
                # ringing after its session is no longer accepting legs.
                await cancel_call(call_sid=sid)
                await dialer_parallel.update_parallel_leg(
                    child_call_sid=sid, leg_id=leg_id, status="cancelled"
                )
                return None
            return sid

        parallel_results = await asyncio.gather(
            *(originate_parallel_target(target) for target in parallel_targets),
            return_exceptions=True,
        )
        child_call_sids = [value for value in parallel_results if isinstance(value, str)]
        if len(child_call_sids) != parallel_line_count:
            session = await dialer_parallel.parallel_session(parent_call_sid=parent_call_sid)
            if not (session or {}).get("winning_child_call_sid"):
                logger.error(f"voice-connect could not create a usable parallel batch for {parent_call_sid}")
                await dialer_parallel.finish_parallel_session(
                    parent_call_sid=parent_call_sid, status="failed"
                )
                await asyncio.gather(*(cancel_call(call_sid=sid) for sid in child_call_sids))
                await update_dialer_call_status(
                    parent_call_sid=parent_call_sid,
                    child_call_sid=None,
                    status="failed",
                    duration_seconds=0,
                )
                return HTMLResponse(
                    content='<?xml version="1.0" encoding="UTF-8"?><Response><Say>We could not connect that call. Please try again.</Say><Hangup/></Response>',
                    media_type="application/xml",
                )
            logger.warning(
                f"Parallel batch {parent_call_sid} was partial, but its first human already won; keeping that call alive"
            )
    else:
        lead_join_url = (
            f"{backend_endpoint}/api/v1/telephony/dialer-conference-join?"
            + urlencode(common_join_fields)
        )
        lead_status_callback_url = status_callback_base + "?" + urlencode({
            "parent_call_sid": parent_call_sid,
        })
        child_call_sid = await dial_lead_into_conference(
            parent_call_sid=parent_call_sid,
            lead_number=to_number,
            caller_id=caller_id,
            join_conference_url=lead_join_url,
            status_callback_url=lead_status_callback_url,
            # Detect on every Twilio outbound call so a rep can request a
            # server-side manual drop while it is still ringing. The saved
            # auto-drop preference controls PLAYBACK, not whether we identify
            # a mailbox; playback requires a confirmed beep or greeting-ending
            # silence (with a six-second grace period for the latter).
            machine_detection=(team_voicemail_policy or {}).get("machine_detection_enabled") is not False,
            machine_detection_preset=str((team_voicemail_policy or {}).get("machine_detection_preset") or "balanced"),
            # One team setting for how long a single call rings (default 30 s).
            ringing_timeout_seconds=call_stages.no_answer_seconds(team_voicemail_policy),
        )
        if not child_call_sid:
            logger.error(f"voice-connect failed to dial lead into conference for {parent_call_sid}")
            await update_dialer_call_status(
                parent_call_sid=parent_call_sid,
                child_call_sid=None,
                status="failed",
                duration_seconds=0,
            )
            return HTMLResponse(
                content='<?xml version="1.0" encoding="UTF-8"?><Response><Say>We could not connect that call. Please try again.</Say><Hangup/></Response>',
                media_type="application/xml",
            )
        await update_dialer_call_child_sid(
            parent_call_sid=parent_call_sid, child_call_sid=child_call_sid
        )
        # A rep who ends the call while it still rings (or whose browser leg
        # never connects) never joins the conference, so no conference event
        # fires and the lead kept ringing: anyone who answered got silence.
        background_tasks.add_task(
            _cancel_lead_if_rep_leaves,
            parent_call_sid=parent_call_sid,
            child_call_sid=child_call_sid,
            ring_seconds=call_stages.no_answer_seconds(team_voicemail_policy),
        )

    # No <Say> on the rep's leg: the disclosure belongs on the lead's leg
    # (see handle_dialer_conference_join), both so the called party actually
    # hears it and so the rep joins sub-second instead of missing a fast
    # answerer's "Hello?" behind ~4s of speech.
    # beep="false" everywhere - the old <Dial><Number> bridge had no join
    # tones, and a later task has a manager joining silently to listen.
    # Live, speaker-attributed captions for the coach: a Media Stream on the
    # rep's own leg (inbound track = rep, outbound track = the conference mix).
    # Before <Dial> so the first "Hello?" is captured. "" whenever it is not
    # configured, which leaves the TwiML exactly as it was.
    live_stream = await _live_transcription_twiml(
        backend_endpoint=backend_endpoint, channel_key=parent_call_sid,
        leg_call_sid=parent_call_sid,
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        + live_stream
        + "<Dial>"
        "<Conference "
        + _conference_telemetry_attributes(
            events_url=conference_events_url, recording_url=recording_callback_url
        )
        + 'startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">'
        f"{escape(conference_name)}</Conference>"
        "</Dial>"
        "</Response>"
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


async def _cancel_parallel_siblings(
    *, parent_call_sid: str, winning_child_call_sid: str
) -> None:
    """Cancel known losing lines after Twilio receives the winner's bridge TwiML."""
    try:
        siblings = await dialer_parallel.parallel_legs(parent_call_sid=parent_call_sid)
        await asyncio.gather(*(
            cancel_call_if_ringing(call_sid=str(leg["child_call_sid"]))
            for leg in siblings
            if leg.get("child_call_sid")
            and leg.get("child_call_sid") != winning_child_call_sid
            and leg.get("status") not in {
                "completed", "busy", "no-answer", "failed", "cancelled", "machine",
                "voicemail", "abandoned_early",
            }
        ), return_exceptions=True)
    except Exception as exc:  # noqa: BLE001 - webhook cleanup must not strand the winner
        logger.error(f"Could not cancel losing lines for parallel call {parent_call_sid}: {exc}")


async def _expire_parallel_overflow_offer(
    *, child_call_sid: str, parent_call_sid: str = "", leg_id: str = "", account_sid: str = "",
) -> None:
    """After eight seconds, an unclaimed teammate offer falls back to the agent."""
    await asyncio.sleep(8)
    try:
        await dialer_overflow_recovery.recover_overflow_offers(child_call_sid=child_call_sid)
    except Exception as exc:
        logger.warning(f"Parallel overflow timer deferred to recovery worker: {type(exc).__name__}")


@router.websocket("/dialer-agent-media/{workflow_run_id}")
async def handle_dialer_agent_media(websocket: WebSocket, workflow_run_id: int):
    await websocket.accept()
    await dialer_agent_handoff.serve_agent_media(websocket, workflow_run_id)


@router.get("/dialer/parallel-agent-capability")
async def get_parallel_agent_capability(user=Depends(require_sales_dialer_role)):
    client = await _twilio_client()
    return await dialer_agent_handoff.agent_handoff_capability(
        rep_user_id=str(user.provider_id or ""),account_sid=str(client.account_sid if client else ""),
    )


@router.post("/dialer-agent-stream-status", include_in_schema=False)
async def handle_dialer_agent_stream_status(request: Request, workflow_run_id: int):
    form_data = dict(await request.form())
    if not await _verify_twilio_signature(request, form_data):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    if form_data.get("StreamEvent") != "stream-started":
        return {"status": "ignored"}
    try:
        # Stream callbacks use ISO 8601, unlike call/conference RFC timestamps.
        stamp = datetime.fromisoformat(str(form_data.get("Timestamp") or "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Timestamp must include its timezone")
    except ValueError:
        raise HTTPException(status_code=400, detail="Missing provider stream timestamp")
    call_id = str(form_data.get("CallSid") or "")
    if not _TWILIO_CALL_SID_PATTERN.fullmatch(call_id):
        raise HTTPException(status_code=400, detail="Invalid provider call")
    try:
        recorded = await dialer_agent_handoff.record_handoff_event(
            call_id=call_id,event="started",event_at=stamp.astimezone(UTC).isoformat(),
            workflow_run_id=str(workflow_run_id),
        )
    except dialer_agent_handoff.AgentHandoffUnavailable:
        raise HTTPException(status_code=503, detail="Could not persist the agent connection")
    if not recorded:
        raise HTTPException(status_code=409, detail="The stream does not match a reserved handoff")
    return {"status": "success"}


@router.post("/dialer-parallel-hold-next",include_in_schema=False)
async def handle_parallel_hold_next(request: Request,parent_call_sid: str,leg_id: UUID):
    form = dict(await request.form())
    if not await _verify_twilio_signature(request,form):
        raise HTTPException(status_code=401,detail="Invalid webhook signature")
    child = str(form.get("CallSid") or "")
    if not _TWILIO_CALL_SID_PATTERN.fullmatch(child) or not _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid):
        raise HTTPException(status_code=400,detail="Invalid Hold call identity")
    try:
        twiml = await dialer_hold.hold_twiml(parent_call_sid=parent_call_sid,child_call_sid=child,
            leg_id=str(leg_id),account_sid=str(form.get("AccountSid") or ""))
    except dialer_agent_handoff.AgentHandoffUnavailable:
        raise HTTPException(status_code=503,detail="Could not resolve the held call")
    return HTMLResponse(content=twiml or dialer_hold.HANGUP,media_type="application/xml")


@router.post("/dialer-extra-answer-wait", include_in_schema=False)
async def handle_extra_answer_wait(request: Request):
    """Conference waitUrl for an extra answer / ring-back: the original rep's hello clip
    (else a neutral "Hi, one moment.") while the softphone joins. Never hold music."""
    form = dict(await request.form())
    if not await _verify_twilio_signature(request, form):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    # The person's own call: named in the (signed) waitUrl, else the request's CallSid.
    call_sid = str(request.query_params.get("call") or form.get("CallSid") or "")
    if not _TWILIO_CALL_SID_PATTERN.fullmatch(call_sid):
        return HTMLResponse(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Pause length="20"/></Response>',
            media_type="application/xml",
        )
    return HTMLResponse(content=await dialer_extra_answer.hello_wait_twiml(call_sid), media_type="application/xml")


@router.post("/dialer-conference-join", include_in_schema=False)
async def handle_dialer_conference_join(
    request: Request,
    background_tasks: BackgroundTasks,
    conference_name: str = "",
    muted: str = "false",
    end_on_exit: str = "true",
    start_on_enter: str = "true",
    events_url: str = "",
    recording_url: str = "",
    parent_call_sid: str = "",
    parallel_leg_id: str = "",
    parallel_entry_id: str = "",
    handled: str = "",
):
    """Default end_on_exit="true" is deliberately the fail-SAFE default, even
    though voice-connect (its only caller) always passes it explicitly. If
    that param were ever dropped in transit, "false" would resurrect a nasty
    bug: the lead hangs up, the conference stays alive with the rep alone,
    the Voice SDK never fires `disconnect`, and the rep's UI sits there with
    a running timer and no disposition prompt. A listen-in leg that must NOT
    end the conference has to pass end_on_exit=false explicitly."""
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-conference-join webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    if not conference_name:
        logger.error("dialer-conference-join missing conference_name")
        return HTMLResponse(content=hangup, media_type="application/xml")

    # Twilio's synchronous DetectMessageEnd result arrives on the signed
    # TwiML request. Only a confirmed beep, explicit rep opt-in and a current
    # WAV recording can divert the call into automatic playback.
    answered_by = str(form_data.get("AnsweredBy") or "").strip().lower()
    # Twilio reports how long its AMD took to decide on the same signed request.
    amd_decision_ms = form_data.get("MachineDetectionDuration")
    parallel_call_sid = str(form_data.get("CallSid") or "")
    # Dialer Phases 2 + 3: the handling agent hands the leg back through this same
    # signed URL with handled=person|timeout (a person / the fail-safe: to the rep) or
    # handled=machine (a mailbox: Phase 1 voicemail rules). Only our server builds it.
    handled = handled if handled in {"person", "machine", "timeout", "self"} else ""
    if handled == "self":
        # The rep chose to leave the voicemail themselves and is joined before
        # detection decides: no verdict to record, and no notice into a mailbox.
        answered_by = ""
    elif handled:
        answered_by = "machine_end_beep" if handled == "machine" else "human"
    elif call_sorter.call_sorter_enabled():
        handling_ref = parallel_call_sid if parallel_leg_id else parent_call_sid
        if _TWILIO_CALL_SID_PATTERN.fullmatch(handling_ref or "") and await call_handling.is_live(handling_ref):
            # The agent is working this line (screener / menu): it must not join the
            # rep's room behind the agent's back. Wait; the agent hands it on.
            fallback = str(request.url) + ("&" if "?" in str(request.url) else "?") + "handled=timeout"
            return HTMLResponse(
                content='<?xml version="1.0" encoding="UTF-8"?><Response><Pause length="90"/>'
                        f'<Redirect method="POST">{escape(fallback)}</Redirect></Response>',
                media_type="application/xml",
            )
    # F5 (only with SYSEVO_DIALER_CALL_SORTER): what the call sorter heard decides
    # an unsure AMD, and can veto a machine before anything plays.
    sorter_mailbox_full = False
    if not handled and answered_by in _MACHINE_END_RESULTS | {"unknown"} and call_sorter.call_sorter_enabled():
        sorter_ref = parallel_call_sid if parallel_leg_id else parent_call_sid
        decision = await sorter_gate.consult_sorter(
            sorter_ref if _TWILIO_CALL_SID_PATTERN.fullmatch(sorter_ref or "") else None,
            amd="unsure" if answered_by == "unknown" else "machine",
        )
        if decision.kind == "machine" and answered_by == "unknown":
            answered_by = "machine_end_other"
        elif decision.kind == "person" and answered_by != "unknown":
            # A screener asking who is calling (or a person) is never a voicemail.
            answered_by = "human"
        sorter_mailbox_full = decision.mailbox_full and answered_by in _MACHINE_END_RESULTS
    if parallel_leg_id:
        try:
            UUID(parallel_leg_id)
            UUID(parallel_entry_id)
        except (ValueError, TypeError):
            return HTMLResponse(content=hangup, media_type="application/xml")
        if (
            not parent_call_sid
            or not _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid)
            or parent_call_sid_from_conference_name(conference_name) != parent_call_sid
            or not _TWILIO_CALL_SID_PATTERN.fullmatch(parallel_call_sid)
        ):
            logger.warning("Rejected parallel conference join with mismatched call identity")
            return HTMLResponse(content=hangup, media_type="application/xml")
        if answered_by and amd_decision_ms not in (None, ""):
            background_tasks.add_task(
                record_twilio_amd_decision,
                parent_call_sid=parent_call_sid, decision_ms=amd_decision_ms,
                child_call_sid=parallel_call_sid, leg_id=parallel_leg_id,
            )

        # Twilio can return `unknown` when AMD times out without confidence.
        # The dialer policy is to prefer a real connect over hiding a person,
        # so an unknown result gets the same atomic human-claim path. Keep the
        # stored leg classification as human because it has now been admitted
        # to the rep/teammate bridge.
        parallel_answered_by = "human" if answered_by in {"human", "unknown"} else answered_by
        if parallel_answered_by == "human":
            won = await dialer_parallel.claim_parallel_winner(
                parent_call_sid=parent_call_sid,
                entry_id=parallel_entry_id,
                child_call_sid=parallel_call_sid,
                leg_id=parallel_leg_id,
            )
            if not won:
                # Dialer Phase 1b: a second person answered while the rep is already
                # talking. Hold is gone: the extra-answer chain connects a free
                # teammate, the voice agent, or plays the rep's apology. The person
                # never hears music, silence or "please hold".
                endpoint_root = str(request.url).split("?", 1)[0].rsplit("/", 1)[0]
                twiml = await dialer_extra_answer.first_answer_twiml(
                    parent_call_sid=parent_call_sid, child_call_sid=parallel_call_sid,
                    leg_id=parallel_leg_id, account_sid=str(form_data.get("AccountSid") or ""),
                    root=endpoint_root,
                )
                return HTMLResponse(content=twiml, media_type="application/xml")
            else:
                # The claim RPC has already linked entry, destination, caller ID
                # and status atomically. No delayed PATCH may revive this parent.
                # Return winner TwiML immediately; cancel siblings off the
                # critical webhook path after Twilio joins this conference.
                background_tasks.add_task(
                    _cancel_parallel_siblings,
                    parent_call_sid=parent_call_sid,
                    winning_child_call_sid=parallel_call_sid,
                )
        elif answered_by.startswith("machine"):
            await dialer_parallel.update_parallel_leg(
                child_call_sid=parallel_call_sid,
                leg_id=parallel_leg_id,
                status="machine",
                answered_by=answered_by,
            )
            if answered_by not in {"machine_end_beep", "machine_end_silence", "machine_end_other"}:
                return HTMLResponse(content=hangup, media_type="application/xml")
        else:
            # No AMD classification means it is not safe to bridge this leg.
            await dialer_parallel.update_parallel_leg(
                child_call_sid=parallel_call_sid,
                leg_id=parallel_leg_id,
                status="failed",
                answered_by=answered_by or "unknown",
            )
            return HTMLResponse(content=hangup, media_type="application/xml")

    elif answered_by:
        # CallSid is present on Twilio's signed answer webhook, so write it
        # before AMD state. A very fast answer can beat the calls.create
        # response that ordinarily fills child_call_sid.
        lead_sid = str(form_data.get("CallSid") or "")
        if parent_call_sid and lead_sid and _TWILIO_CALL_SID_PATTERN.fullmatch(lead_sid):
            await update_dialer_call_child_sid(
                parent_call_sid=parent_call_sid,
                child_call_sid=lead_sid,
            )
            await update_dialer_call_amd_result(
                parent_call_sid=parent_call_sid,
                answered_by=answered_by,
            )
            if amd_decision_ms not in (None, ""):
                background_tasks.add_task(
                    record_twilio_amd_decision,
                    parent_call_sid=parent_call_sid, decision_ms=amd_decision_ms,
                )

    if (
        sorter_mailbox_full
        and parent_call_sid
        and _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid)
        and parent_call_sid_from_conference_name(conference_name) == parent_call_sid
    ):
        # The mailbox is full (the sorter heard it say so): nothing can be left.
        # Never report a voicemail; end the line and, on a single call, the rep's leg.
        await record_voicemail_playback_failure(
            parent_call_sid=parent_call_sid, reason="mailbox_full",
            child_call_sid=parallel_call_sid if parallel_leg_id else None,
            leg_id=parallel_leg_id or None,
        )
        background_tasks.add_task(
            call_stages.record_call_stage,
            parallel_call_sid if parallel_leg_id else parent_call_sid, "mailbox_full", {"source": "sorter"},
        )
        if not parallel_leg_id:
            background_tasks.add_task(_release_rep_for_voicemail, parent_call_sid)
        return HTMLResponse(content=hangup, media_type="application/xml")

    if (
        answered_by in {"machine_end_beep", "machine_end_silence", "machine_end_other"}
        and parent_call_sid
        and _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid)
        and parent_call_sid_from_conference_name(conference_name) == parent_call_sid
    ):
        call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        rep_user_id = (call_row or {}).get("rep_user_id")
        child_sid = str(form_data.get("CallSid") or "")
        if isinstance(rep_user_id, str) and child_sid:
            # A manual press belongs to the rep's single call. A parallel leg
            # follows the team's automatic policy only: treating a press on
            # the aggregate as "manual" made every machine leg fail the claim
            # (no dialer_calls row for that child) and hang up silently.
            manual_requested = (
                not parallel_leg_id
                and bool((call_row or {}).get("manual_voicemail_requested_at"))
            )
            call_entry_id = parallel_entry_id or None
            policy = await get_team_voicemail_policy(
                user_id=rep_user_id, parent_call_sid=parent_call_sid, entry_id=call_entry_id,
            )
            on_machine = str((policy or {}).get("on_machine") or "drop_vm")
            # Single call: the rep's own "Send my voicemail automatically"
            # choice (else the team default). Parallel line: always, when the
            # team action is drop_vm. Both resolved server-side for this rep.
            enabled, playback_url, drop_id = await get_auto_voicemail_settings(
                user_id=rep_user_id,
                allow_manual_when_disabled=manual_requested,
                parent_call_sid=parent_call_sid,
                entry_id=call_entry_id,
                parallel=bool(parallel_leg_id),
            )
            # Twilio <Play> supports WAV/MP3. Old recordings in browser
            # containers must continue into the conference rather than
            # pretending a silent message was sent.
            is_wav = bool(playback_url and playback_url.split("?", 1)[0].lower().endswith(".wav"))
            should_play = manual_requested or (enabled and on_machine == "drop_vm")
            endpoint_root = str(request.url).split("?", 1)[0].rsplit("/", 1)[0]
            if should_play and playback_url and is_wav:
                # One database claim covers both paths: when the API request
                # and this signed answer callback race, exactly one can start
                # the recording. Auto and manual requests have separate
                # predicates so an opt-in does not masquerade as a rep press.
                claimed = await claim_manual_voicemail_playback(
                    parent_call_sid=parent_call_sid,
                    child_call_sid=child_sid,
                    manual=manual_requested,
                )
                if claimed:
                    if drop_id:
                        await attach_voicemail_drop_to_call(
                            parent_call_sid=parent_call_sid,
                            child_call_sid=child_sid,
                            drop_id=drop_id,
                        )
                    if parallel_leg_id:
                        await dialer_parallel.update_parallel_leg(
                            child_call_sid=parallel_call_sid,
                            leg_id=parallel_leg_id,
                            status="voicemail",
                            answered_by=answered_by,
                        )
                    twiml = _voicemail_playback_twiml(
                        endpoint_root=endpoint_root,
                        parent_call_sid=parent_call_sid,
                        playback_url=playback_url,
                        pause_seconds=_VOICEMAIL_START_PAUSE_SECONDS,
                        parallel_leg_id=parallel_leg_id,
                        drop_id=drop_id,
                        leg_call_sid=child_sid,
                    )
                    if not parallel_leg_id:
                        # The prospect leg now owns the message. Release the
                        # rep straight away (after this TwiML is returned) so
                        # they go to wrap-up and the next lead while it plays.
                        background_tasks.add_task(_release_rep_for_voicemail, parent_call_sid)
                    stage_key = child_sid if parallel_leg_id else parent_call_sid
                    background_tasks.add_task(
                        call_stages.record_call_stage, stage_key, "machine", {"answered_by": answered_by},
                    )
                    background_tasks.add_task(
                        call_stages.record_call_stage, stage_key, "vm_playing",
                        {"source": "manual" if manual_requested else "auto", "drop_id": drop_id},
                    )
                    return HTMLResponse(content=twiml, media_type="application/xml")
                # Twilio retries a TwiML fetch it did not receive. When the
                # first attempt already claimed this exact leg, replay the same
                # playback instead of bridging a mailbox into the conference.
                replay_row = call_row
                if parallel_leg_id:
                    replay_row = next((
                        leg for leg in await dialer_parallel.parallel_legs(parent_call_sid=parent_call_sid)
                        if leg.get("leg_id") == parallel_leg_id and leg.get("child_call_sid") == child_sid
                    ), None)
                else:
                    replay_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
                    if (replay_row or {}).get("child_call_sid") != child_sid:
                        replay_row = None
                if _voicemail_playback_in_progress(replay_row):
                    logger.info(f"Replaying the claimed voicemail on {child_sid} for a retried join")
                    return HTMLResponse(content=_voicemail_playback_twiml(
                        endpoint_root=endpoint_root,
                        parent_call_sid=parent_call_sid,
                        playback_url=playback_url,
                        pause_seconds=_VOICEMAIL_START_PAUSE_SECONDS,
                        parallel_leg_id=parallel_leg_id,
                        drop_id=drop_id,
                        leg_call_sid=child_sid,
                    ), media_type="application/xml")
            # Hang up is an explicit manager choice, and a parallel machine
            # line is never bridged. On a single call with Drop VM that did not
            # play automatically (the rep chose to be asked, or no playable
            # recording), the mailbox is passed to the rep: they get the
            # one-tap "Send my voicemail" prompt or can speak live.
            if not manual_requested:
                if on_machine == "hangup" or (parallel_leg_id and on_machine == "drop_vm"):
                    return HTMLResponse(content=hangup, media_type="application/xml")
                if not parallel_leg_id:
                    # The mailbox goes to the rep, who is still on the line (mode
                    # "self", or "ask" before a choice). 'machine' is recorded at the
                    # AMD decision; 'beep' only for a real tone or a greeting that
                    # ended (machine_end_other is AMD giving up, not a beep).
                    for stage, detail in _amd_machine_stages(
                        answered_by, mode=(policy or {}).get("voicemail_mode"),
                    ):
                        background_tasks.add_task(call_stages.record_call_stage, parent_call_sid, stage, detail)

    if (
        not parallel_leg_id
        and parent_call_sid
        and _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid)
        and parent_call_sid_from_conference_name(conference_name) == parent_call_sid
    ):
        # The rep left while a queued manual voicemail kept this destination
        # ringing. Whoever answers can no longer reach the rep: never drop a
        # person (or an unplayable machine) into an empty conference.
        evidence = await get_voicemail_drop_evidence(parent_call_sid=parent_call_sid)
        detached = bool(evidence.get("rep_detached_at"))
        # The rep's own leg already ended (their browser call failed to connect,
        # or they hung up while it rang) but this destination still answered.
        # Joining would leave the prospect alone in a silent, unrecorded room
        # (21 Oct: one sat there for 1m55s). Treat it like a rep who moved on.
        rep_gone = not detached and await _rep_leg_ended(parent_call_sid)
        if detached or rep_gone:
            lead_sid = str(form_data.get("CallSid") or "")
            if (
                detached
                and not answered_by
                and call_sorter.call_sorter_enabled()
                and _TWILIO_CALL_SID_PATTERN.fullmatch(lead_sid)
            ):
                # Machine detection is off for this account, so Twilio says nothing about
                # who answered. The call sorter decides: a mailbox is held (silently, the
                # rep has gone) and the recording plays when the greeting ends.
                decision = await sorter_gate.consult_sorter(parent_call_sid, amd="unsure")
                if decision.kind == "machine":
                    if decision.mailbox_full:
                        await record_voicemail_playback_failure(
                            parent_call_sid=parent_call_sid, reason="mailbox_full",
                        )
                        background_tasks.add_task(
                            call_stages.record_call_stage, parent_call_sid, "mailbox_full", {"source": "sorter"},
                        )
                        return HTMLResponse(content=hangup, media_type="application/xml")
                    background_tasks.add_task(
                        call_stages.record_call_stage, parent_call_sid, "machine", {"source": "sorter"},
                    )
                    background_tasks.add_task(
                        _play_queued_voicemail_after_greeting,
                        parent_call_sid=parent_call_sid, child_call_sid=lead_sid,
                        endpoint_root=str(request.url).split("?", 1)[0].rsplit("/", 1)[0],
                    )
                    return HTMLResponse(content=_greeting_wait_twiml(), media_type="application/xml")
            if (
                not answered_by.startswith("machine") and answered_by != "fax"
                and _TWILIO_CALL_SID_PATTERN.fullmatch(lead_sid)
            ):
                # A person picked up after the rep handed the line over and moved on.
                # Ring the original rep back when they are free; the recording is
                # never played to a person.
                endpoint_root = str(request.url).split("?", 1)[0].rsplit("/", 1)[0]
                ringback = await dialer_voicemail_ringback.offer_ringback_twiml(
                    parent_call_sid=parent_call_sid, prospect_call_sid=lead_sid,
                    endpoint_root=endpoint_root,
                )
                if ringback:
                    logger.info(f"Ringing the rep back for {parent_call_sid}: a person answered after the hand-off")
                    background_tasks.add_task(
                        dialer_voicemail_ringback.expire_ringback,
                        parent_call_sid=parent_call_sid, prospect_call_sid=lead_sid,
                    )
                    return HTMLResponse(content=ringback, media_type="application/xml")
                # The rep is busy, offline or still wrapping up: start the extra-answer
                # chain straight away (teammate → agent → apology / goodbye) instead of
                # hanging up on the person.
                chain_twiml = await dialer_extra_answer.ringback_unavailable_twiml(
                    parent_call_sid=parent_call_sid, prospect_call_sid=lead_sid,
                    account_sid=str(form_data.get("AccountSid") or ""), root=endpoint_root,
                )
                if chain_twiml:
                    # The chain recorded 'extra_answer' on this call already.
                    logger.info(f"Ring-back not possible for {parent_call_sid}: the extra-answer chain took the call")
                    return HTMLResponse(content=chain_twiml, media_type="application/xml")
            logger.info(
                f"Rep {'detached' if detached else 'leg ended'} before {parent_call_sid} answered "
                f"({answered_by or 'no AMD'}); ending the leg"
            )
            if rep_gone and not answered_by.startswith("machine") and answered_by != "fax":
                background_tasks.add_task(
                    call_stages.record_call_stage, parent_call_sid, "person",
                    {"rep_gone": True, "ringback": "unavailable"},
                )
            elif not answered_by.startswith("machine") and answered_by != "fax":
                # The rep is busy (or offline): today's behaviour. Record it so the
                # rep's background status says "call them back" rather than "no
                # mailbox", and the call row shows what happened.
                await record_voicemail_playback_failure(
                    parent_call_sid=parent_call_sid, reason="person_answered",
                )
                background_tasks.add_task(
                    call_stages.record_call_stage, parent_call_sid, "person",
                    {"after_vm_request": True, "ringback": "unavailable"},
                )
            return HTMLResponse(content=hangup, media_type="application/xml")
        if answered_by in {"human", "unknown"}:
            background_tasks.add_task(
                call_stages.record_call_stage, parent_call_sid, "person", {"answered_by": answered_by},
            )

    known_machine_answers = {
        "machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax",
    }
    if parallel_leg_id and answered_by in known_machine_answers:
        # A machine greeting without an opted-in, usable recording is never
        # connected to a rep. The attempt remains separately recorded in the
        # parallel leg ledger for disposition/reconciliation.
        return HTMLResponse(content=hangup, media_type="application/xml")

    # voice-connect passes these so this leg can repeat the conference-level
    # attributes - see _conference_telemetry_attributes. Absent (or not
    # absolute) they're simply omitted, which is exactly how this leg behaved
    # before, so an older in-flight join URL during a deploy still works.
    telemetry_attributes = _conference_telemetry_attributes(
        events_url=events_url, recording_url=recording_url
    )
    if not telemetry_attributes:
        logger.warning(
            f"dialer-conference-join for '{conference_name}' has no usable callback URLs - "
            "this leg carries no conference recording/status attributes, so if it creates "
            "the conference there will be no recording and no conference-start event"
        )

    # The recording/monitoring disclosure lives here, on the lead's own leg,
    # rather than on the rep's leg in voice-connect - this is the leg the
    # called party is actually listening to. A manager's listen-in leg does
    # NOT come through here (it has its own dialer-listen-connect endpoint),
    # so this never announces a silent monitor.
    # Never to a mailbox: when the rep leaves a voicemail themselves (ask/self
    # modes hand the mailbox to them), the notice was recorded as the first words
    # of their message. It is for a person on the line, not a recording.
    disclosure = (
        "" if answered_by.startswith("machine") or answered_by == "fax" or handled == "self"
        else "<Say>This call may be recorded and monitored for quality assurance.</Say>"
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        + disclosure
        + "<Dial>"
        f"<Conference {telemetry_attributes}muted={quoteattr(muted)} "
        f"endConferenceOnExit={quoteattr(end_on_exit)} "
        f'startConferenceOnEnter={quoteattr(start_on_enter)} beep="false">'
        f"{escape(conference_name)}</Conference>"
        "</Dial></Response>"
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.post("/dialer-auto-voicemail-complete", include_in_schema=False)
async def handle_dialer_auto_voicemail_complete(
    request: Request, parent_call_sid: str = "", parallel_leg_id: str = "",
    vm_started: str = "", vm_expect: str = "",
):
    """Finish the lead leg and the rep's waiting conference after full playback."""
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())
    if (
        not await _verify_twilio_signature(request, form_data)
        or not _TWILIO_CALL_SID_PATTERN.fullmatch(parent_call_sid)
    ):
        logger.warning("Rejected invalid automatic voicemail completion callback")
        return HTMLResponse(content=hangup, media_type="application/xml", status_code=403)

    child_call_sid = form_data.get("CallSid")
    if parallel_leg_id:
        try:
            UUID(parallel_leg_id)
        except (ValueError, TypeError):
            return HTMLResponse(content=hangup, media_type="application/xml", status_code=403)
        legs = await dialer_parallel.parallel_legs(parent_call_sid=parent_call_sid)
        matching_leg = next((leg for leg in legs if leg.get("leg_id") == parallel_leg_id), None)
        if not matching_leg or child_call_sid != matching_leg.get("child_call_sid"):
            logger.warning("Rejected automatic voicemail completion for an unrelated parallel leg")
            return HTMLResponse(content=hangup, media_type="application/xml", status_code=403)
        await dialer_parallel.update_parallel_leg(
            child_call_sid=child_call_sid, leg_id=parallel_leg_id, status="voicemail",
        )
    else:
        call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        if not call_row or child_call_sid != call_row.get("child_call_sid"):
            logger.warning("Rejected automatic voicemail completion for an unrelated Twilio call")
            return HTMLResponse(content=hangup, media_type="application/xml", status_code=403)

    # This endpoint is reached sequentially after the TwiML <Play> verb. A
    # recording Twilio could not fetch or decode is skipped and lands here
    # early, so the signed expectation decides between "sent" and "failed".
    # Either way the destination is a machine: end it, and the rep's leg.
    stage_key = str(child_call_sid) if parallel_leg_id and child_call_sid else parent_call_sid
    if _playback_finished(vm_started=vm_started, vm_expect=vm_expect):
        await mark_auto_voicemail_sent(
            parent_call_sid=parent_call_sid,
            child_call_sid=str(child_call_sid) if parallel_leg_id and child_call_sid else None,
        )
        await call_stages.record_call_stage(stage_key, "vm_left", {})
    else:
        # Reaching the Redirect early means <Play> skipped the file while the call
        # stayed up (unplayable audio). A full mailbox instead hangs up mid-message,
        # so the Redirect never runs; that is caught by the leg's status callback
        # (_record_single_call_status_stage) as `mailbox_full`.
        logger.warning(f"Voicemail on {parent_call_sid} reached its completion before the recording could finish")
        await record_voicemail_playback_failure(
            parent_call_sid=parent_call_sid,
            reason="playback_incomplete",
            child_call_sid=str(child_call_sid) if parallel_leg_id and child_call_sid else None,
            leg_id=parallel_leg_id or None,
        )
    if parallel_leg_id:
        session = await dialer_parallel.parallel_session(parent_call_sid=parent_call_sid)
        legs = await dialer_parallel.parallel_legs(parent_call_sid=parent_call_sid)
        if (
            session
            and not session.get("winning_child_call_sid")
            and _parallel_batch_has_settled(legs)
        ):
            await dialer_parallel.finish_parallel_session(parent_call_sid=parent_call_sid, status="failed")
            await update_dialer_call_status(
                parent_call_sid=parent_call_sid,
                child_call_sid=None,
                status="failed",
                duration_seconds=0,
            )
            await cancel_call(call_sid=parent_call_sid)
    else:
        await cancel_call(call_sid=parent_call_sid)
    return HTMLResponse(content=hangup, media_type="application/xml")


async def _cancel_orphaned_lead_leg(parent_call_sid: str) -> None:
    """End the lead's leg if the conference ended before they ever joined it.

    The rep's leg carries endConferenceOnExit="true", so a rep hanging up (or
    skipping on to the next lead) ends the conference - but that only drops
    its *participants*. A lead whose phone is still ringing has not joined
    yet, so their leg survives, they answer, dialer-conference-join drops
    them into a freshly-recreated empty conference, and they hear silence:
    an abandoned call placed from our own caller ID. The old <Dial><Number>
    bridge cancelled that leg for us. See dialer_conference.cancel_call.

    Fail-soft on its own terms, not merely by delegation: the two helpers it
    calls each swallow their own errors, but the code BETWEEN them is this
    function's, and an exception there would escape into the webhook and
    become a 500 that Twilio retries. The blanket except is what makes the
    guarantee independent of either callee's future contract.
    """
    try:
        parallel_session = await dialer_parallel.parallel_session(parent_call_sid=parent_call_sid)
        if parallel_session:
            legs = await dialer_parallel.parallel_legs(parent_call_sid=parent_call_sid)
            independent = await dialer_hold.independent_children(parent_call_sid)
            winning_sid = parallel_session.get("winning_child_call_sid")
            await asyncio.gather(*(
                # Recheck provider state: a sibling can answer while this
                # cleanup is reading its durable handoff reservation.
                cancel_call_if_ringing(call_sid=str(leg["child_call_sid"]))
                for leg in legs
                if leg.get("child_call_sid")
                and leg.get("child_call_sid") not in independent
                and leg.get("child_call_sid") != winning_sid
                and leg.get("status") not in {"completed", "busy", "no-answer", "failed", "cancelled", "machine"}
            ))

        leg = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)

        if leg is None:
            # No dialer_calls row at all. Expected, not an anomaly: rows are
            # only created for a recognized rep identity, so an unrecognized
            # caller legitimately has none - and a row we never wrote can't
            # have a lead leg we need to end.
            logger.debug(
                f"dialer-conference-events: no dialer_calls row for {parent_call_sid}, "
                "nothing to clean up"
            )
            return

        child_call_sid = leg.get("child_call_sid")
        lead_status = (leg.get("status") or "").strip().lower()

        if parallel_session and child_call_sid == parallel_session.get("winning_child_call_sid"):
            # Conference teardown ended the winning participant. Its status
            # callback can arrive later; do not send a second REST hangup.
            return

        if not child_call_sid:
            # The row EXISTS but has no lead SID. voice-connect persists that
            # the moment the dial returns, so by the time a conference can end
            # it should always be there. Reaching this means the write failed
            # and the lead's leg is now unreachable by SID - a real anomaly.
            logger.error(
                f"dialer-conference-events: dialer_calls row for {parent_call_sid} has no "
                "child_call_sid, cannot end a possibly-orphaned lead leg"
            )
            return

        if lead_status in _LEAD_LEG_SETTLED_STATUSES:
            return

        if _voicemail_playback_in_progress(leg):
            # The rep was released on purpose while the prospect leg plays the
            # recording. Ending that leg here would cut the message off; its
            # completion callback hangs it up and records sent/failed.
            logger.info(
                f"dialer-conference-events: keeping {child_call_sid} for its voicemail playback"
            )
            return

        if (
            not parallel_session
            and leg.get("manual_voicemail_requested_at")
            and not leg.get("voicemail_drop_started_at")
            and leg.get("auto_voicemail_sent") is not True
            and await mark_rep_detached_for_voicemail(parent_call_sid=parent_call_sid)
        ):
            # The rep queued Drop VM while this destination was ringing and then
            # moved on. Keep the carrier leg alive so its mailbox can still
            # receive the recording; the join webhook ends it if a person (or
            # no detectable mailbox) answers instead.
            logger.info(
                f"dialer-conference-events: keeping {child_call_sid} ringing for a queued voicemail"
            )
            return

        logger.info(
            f"dialer-conference-events: conference ended with lead leg {child_call_sid} "
            f"at status '{lead_status or 'unknown'}' - ending orphaned leg"
        )
        await cancel_call(call_sid=child_call_sid)
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into a webhook handler
        logger.error(f"Failed orphaned lead leg cleanup for {parent_call_sid}: {exc}")


@router.post("/dialer-conference-events", include_in_schema=False)
async def handle_dialer_conference_events(request: Request):
    """Conference lifecycle callback. Unlike the TwiML-serving endpoints this
    raises 401 on a bad signature rather than returning hangup TwiML - same
    as dialer-call-status and dialer-recording-callback, which are likewise
    pure status callbacks with no live call leg waiting on a response."""
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-conference-events webhook")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = form_data.get("StatusCallbackEvent", "")
    conference_sid = form_data.get("ConferenceSid", "")
    parent_call_sid = parent_call_sid_from_conference_name(form_data.get("FriendlyName", ""))

    # A manual drop pressed while Twilio is still finishing the AMD answer
    # webhook is picked up here after the lead has joined. This closes the
    # answer-webhook/API race without ever guessing from transcript text.
    if event_type == "conference-start" and conference_sid and parent_call_sid:
        await update_dialer_call_conference_sid(
            parent_call_sid=parent_call_sid, conference_sid=conference_sid
        )
    elif event_type in {"participant-join", "join"} and parent_call_sid:
        if conference_sid:
            # Every join carries the ConferenceSid too. Writing it again (same
            # value) means one lost conference-start no longer orphans the
            # recording.
            await update_dialer_call_conference_sid(
                parent_call_sid=parent_call_sid, conference_sid=conference_sid
            )
        child_call_sid = str(form_data.get("CallSid") or form_data.get("ParticipantCallSid") or "")
        event_at = _twilio_event_time(form_data.get("Timestamp"))
        if event_at and child_call_sid and _TWILIO_CALL_SID_PATTERN.fullmatch(child_call_sid):
            parallel_session = await dialer_parallel.parallel_session(
                parent_call_sid=parent_call_sid
            )
            if parallel_session:
                if child_call_sid == parent_call_sid:
                    await dialer_parallel.record_parallel_timing(
                        parent_call_sid=parent_call_sid,
                        child_call_sid=child_call_sid,
                        event_kind="rep_join",
                        event_at=event_at,
                    )
                elif child_call_sid == parallel_session.get("winning_child_call_sid"):
                    await dialer_parallel.record_parallel_timing(
                        parent_call_sid=parent_call_sid,
                        child_call_sid=child_call_sid,
                        event_kind="lead_join",
                        event_at=event_at,
                    )
            else:
                recorded = await record_overflow_join(
                    conference_name=str(form_data.get("FriendlyName") or ""),
                    provider="twilio", participant_call_id=child_call_sid, event_at=event_at,
                )
                if recorded is None:
                    raise HTTPException(status_code=503, detail="Could not persist conference timing")
                # An extra answer's person reached the rep's room: the agent's handover landed.
                await dialer_extra_answer.on_prospect_joined(
                    conference_name=str(form_data.get("FriendlyName") or ""), call_sid=child_call_sid,
                )
        call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        if (
            call_row
            and call_row.get("child_call_sid") == child_call_sid
            and _TWILIO_CALL_SID_PATTERN.fullmatch(child_call_sid)
        ):
            await mark_dialer_lead_joined_conference(
                parent_call_sid=parent_call_sid,
                child_call_sid=child_call_sid,
            )
            call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
            if (
                call_row
                and call_row.get("amd_result") in {"machine_end_beep", "machine_end_silence", "machine_end_other"}
                and call_row.get("manual_voicemail_requested_at")
                and not call_row.get("voicemail_drop_started_at")
                and call_row.get("auto_voicemail_sent") is not True
            ):
                rep_user_id = call_row.get("rep_user_id")
                if isinstance(rep_user_id, str):
                    _, playback_url, drop_id = await get_auto_voicemail_settings(
                        user_id=rep_user_id,
                        allow_manual_when_disabled=True,
                        parent_call_sid=parent_call_sid,
                    )
                    if playback_url and playback_url.split("?", 1)[0].lower().endswith(".wav"):
                        claimed = await claim_manual_voicemail_playback(
                            parent_call_sid=parent_call_sid,
                            child_call_sid=child_call_sid,
                            manual=True,
                        )
                        if claimed:
                            if drop_id:
                                await attach_voicemail_drop_to_call(
                                    parent_call_sid=parent_call_sid,
                                    child_call_sid=child_call_sid,
                                    drop_id=drop_id,
                                )
                            endpoint_root = str(request.url).split("?", 1)[0].rsplit("/", 1)[0]
                            twiml = _voicemail_playback_twiml(
                                endpoint_root=endpoint_root,
                                parent_call_sid=parent_call_sid,
                                playback_url=playback_url,
                                pause_seconds=_VOICEMAIL_START_PAUSE_SECONDS,
                                drop_id=drop_id,
                                leg_call_sid=child_call_sid,
                            )
                            if not await _redirect_twilio_child_to_voicemail(
                                child_call_sid=child_call_sid,
                                twiml=twiml,
                            ):
                                await release_manual_voicemail_playback(
                                    parent_call_sid=parent_call_sid,
                                    child_call_sid=child_call_sid,
                                )
                            else:
                                await _release_rep_for_voicemail(parent_call_sid)
    elif event_type == "conference-end" and parent_call_sid:
        session = await dialer_parallel.parallel_session(parent_call_sid=parent_call_sid)
        if not session:
            # A routed second-pickup conference is named from its child SID, so
            # it has no parallel session of its own. Close its ring reservation.
            await close_inbound_call(provider_call_id=parent_call_sid)
            await evaluate_overflow_guard(provider_call_id=parent_call_sid, provider="twilio")
        if session:
            has_winner = bool(session.get("winning_child_call_sid"))
            final_state = "completed" if has_winner else (
                "failed" if session.get("status") == "failed" else "cancelled"
            )
            await dialer_parallel.finish_parallel_session(
                parent_call_sid=parent_call_sid,
                status=final_state,
            )
            call_row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
            preserve_completed_drop = (
                not has_winner and (call_row or {}).get("status") == "completed"
            )
            if final_state in {"completed", "cancelled"} and not preserve_completed_drop:
                await update_dialer_call_status(
                    parent_call_sid=parent_call_sid,
                    child_call_sid=session.get("winning_child_call_sid"),
                    status="completed" if has_winner else "canceled",
                    duration_seconds=None,
                )
        await _cancel_orphaned_lead_leg(parent_call_sid)

    if event_type in {"participant-leave", "leave"}:
        participant_sid = str(form_data.get("CallSid") or form_data.get("ParticipantCallSid") or "")
        if _TWILIO_CALL_SID_PATTERN.fullmatch(participant_sid):
            closed = await close_floor_monitor_for_provider(participant_sid)
            if closed is None:
                raise HTTPException(status_code=503, detail="Could not persist monitoring participant leave")

    return {"status": "success"}


async def _serve_listen_in_twiml(form_data: dict) -> HTMLResponse:
    """Place an authorized manager in a live call as listener, coach or barge.

    Takes already-parsed, already-signature-verified form data, because it has
    TWO entry points (see handle_voice_connect's delegation and
    handle_dialer_listen_connect below) and re-reading `request.form()` after
    the body has been consumed once would yield nothing. Verifying the
    signature is the caller's job, done exactly once, at the route.

    Listen mode is silent. Whisper mode uses Twilio's conference coaching
    isolation so only the rep hears the manager. Barge is an explicit three-way
    join. All modes suppress the join beep and leave the customer conference
    alive when the manager disconnects.

    - beep="false"                  - joining must not inject an unrequested tone.
    - startConferenceOnEnter="false" - joining a conference that has already
      ended must not resurrect it.
    - endConferenceOnExit="false"   - the manager hanging up must not end the
      call they were monitoring.

    Unlike the pure status-callback endpoints (dialer-conference-events,
    dialer-call-status, dialer-recording-callback) every failure path here
    returns hangup TwiML rather than raising 401/500: there is a live call
    leg waiting on this response, and a non-200 makes Twilio read out its own
    error message - to the manager, on a leg that is about to join a call
    they are supposed to be monitoring silently.

    No _conference_telemetry_attributes here, deliberately. Those exist so
    whichever of the rep/lead legs creates the conference makes it recorded
    and reporting; the manager joins an already-created conference, so their
    copy would be discarded anyway - and resolving them costs a
    get_backend_endpoints() call (which can block on a cloudflared lookup) on
    a path where a human is waiting to hear a live call. The residual race -
    a manager whose leg somehow creates the conference first, costing the
    recording - is accepted knowingly and is on the deployment checklist to
    confirm.
    """
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    # Blanket try, on this function's own terms rather than by delegation.
    # create_dialer_call_listener and is_manager_or_admin each swallow their
    # own errors, but db_client.get_user_by_id does NOT (a DB blip raises)
    # and neither does the string handling around them - and code BETWEEN two
    # individually-protected calls is not itself protected. An escape here
    # becomes a 500, which Twilio both reads aloud and retries.
    try:
        raw_from = form_data.get("From", "")
        parent_call_sid = str(form_data.get("ListenParentCallSid", "")).strip()
        if not parent_call_sid:
            logger.error("listen-in missing ListenParentCallSid")
            return HTMLResponse(content=hangup, media_type="application/xml")

        # Reject malformed SIDs before lookup. The session claim below also
        # requires an active, account-authorized call and supplies its actual
        # conference and rep leg, including for inbound calls.
        if not _TWILIO_CALL_SID_PATTERN.match(parent_call_sid):
            logger.warning("listen-in rejected malformed ListenParentCallSid")
            return HTMLResponse(content=hangup, media_type="application/xml")

        mode = str(form_data.get("MonitorMode", "listen")).strip().lower()
        if mode not in {"listen", "whisper", "barge"}:
            logger.warning("listen-in rejected unsupported monitor mode")
            return HTMLResponse(content=hangup, media_type="application/xml")

        # Same "client:rep-{id}" Voice SDK identity a rep gets: /voice-token
        # issues that shape to everyone holding a sales dialer role, managers
        # and admins included. The helper only extracts a user id - the
        # "rep-" prefix is the token format, not a role claim - and the
        # actual authorization is is_manager_or_admin below.
        manager_id = _parse_rep_id_from_identity(raw_from)
        if manager_id is None:
            logger.warning("listen-in: unrecognized identity")
            return HTMLResponse(content=hangup, media_type="application/xml")

        user = await db_client.get_user_by_id(manager_id)
        if not user or not user.provider_id:
            logger.warning(f"listen-in: no Supabase user for dograh id {manager_id}")
            return HTMLResponse(content=hangup, media_type="application/xml")

        # Authorize BEFORE recording anything and before returning any
        # conference TwiML: an unauthorized caller must leave no
        # dialer_call_listeners row claiming they listened, and must never
        # reach the audio. is_manager_or_admin fails CLOSED, so an error
        # reaching Supabase lands here too.
        #
        # This check is why delegation from voice-connect is routing and not
        # a bypass: BOTH entry points land here, so a sales_rep who hand-
        # crafts a ListenParentCallSid param is rejected on exactly this line.
        if not await is_manager_or_admin(user.provider_id):
            logger.warning(f"listen-in: {user.provider_id} is not a manager/admin")
            return HTMLResponse(content=hangup, media_type="application/xml")

        monitor = await claim_floor_monitor(
            parent_call_sid=parent_call_sid,
            manager_user_id=user.provider_id,
            manager_call_sid=str(form_data.get("CallSid") or ""),
            mode=mode,
            session_id=str(form_data["MonitorSessionId"]) if "MonitorSessionId" in form_data else None,
        )
        if not monitor:
            return HTMLResponse(content=hangup, media_type="application/xml")
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into a webhook handler
        logger.error(f"listen-in failed: {exc}")
        return HTMLResponse(content=hangup, media_type="application/xml")

    # escape() is belt-and-braces now that the SID shape is validated above,
    # and stays for exactly that reason - it must not be the only thing
    # standing between a caller-supplied string and the TwiML document.
    conference_name = monitor["conference_name"]
    conference_attrs = {
        "muted": "true" if mode == "listen" else "false",
        "startConferenceOnEnter": "false",
        "endConferenceOnExit": "false",
        "beep": "false",
    }
    if mode == "whisper":
        # Inbound parent SIDs name the prospect. The server target reader supplies
        # the actual rep SDK leg so Whisper never coaches the prospect instead.
        conference_attrs["coach"] = monitor["coach_call_sid"]
    attrs = " ".join(f'{key}={quoteattr(value)}' for key, value in conference_attrs.items())
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Dial>"
        f"<Conference {attrs}>"
        f"{escape(conference_name)}</Conference>"
        "</Dial></Response>"
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


class FloorMonitorModeRequest(BaseModel):
    session_id: UUID
    mode: Literal["listen", "whisper", "barge"]


@router.post("/dialer/floor-monitor/mode")
async def control_floor_monitor_mode(
    body: FloorMonitorModeRequest, user=Depends(require_sales_dialer_role)
):
    manager_id = getattr(user, "provider_id", None)
    if not isinstance(manager_id, str) or not manager_id:
        raise HTTPException(status_code=503, detail="Your dialer user mapping is unavailable.")
    result = await change_floor_monitor_mode(
        session_id=str(body.session_id), manager_user_id=manager_id, mode=body.mode
    )
    if result == "changed":
        return {"session_id": str(body.session_id), "mode": body.mode}
    if result == "busy":
        raise HTTPException(status_code=409, detail="A mode change is already in progress for this monitor.")
    if result == "unavailable":
        raise HTTPException(status_code=404, detail="This active monitoring session was not found.")
    raise HTTPException(status_code=503, detail="The monitoring mode could not be confirmed. Reconnect and try again.")


@router.post("/dialer-listen-connect", include_in_schema=False)
async def handle_dialer_listen_connect(request: Request):
    """Dedicated listen-in answer URL.

    Currently UNREACHABLE from the browser and kept deliberately: the Voice
    SDK can only dial the single Voice URL configured on the one
    TWILIO_TWIML_APP_SID that generate_voice_access_token grants, which is
    /voice-connect. A Device can pass custom params but cannot choose a
    different URL, so listen-in actually arrives via voice-connect's
    delegation. This route costs nothing, keeps the listen path directly
    addressable and testable, and means pointing a second TwiML App here
    later is a console change with no code change.
    """
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-listen-connect webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    return await _serve_listen_in_twiml(form_data)


@router.post("/dialer-call-status", include_in_schema=False)
async def handle_dialer_call_status(
    request: Request, background_tasks: BackgroundTasks,
    parent_call_sid: str = "", parallel_leg_id: str = "",
):
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-call-status webhook")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if not parent_call_sid:
        logger.warning("dialer-call-status webhook missing parent_call_sid query param")
        return {"status": "ignored", "reason": "missing_parent_call_sid"}

    child_call_sid = form_data.get("CallSid") or None
    call_status = form_data.get("CallStatus", "").strip().lower() or "initiated"
    raw_duration = form_data.get("CallDuration")
    duration_seconds = int(raw_duration) if raw_duration and raw_duration.isdigit() else None

    if parallel_leg_id:
        try:
            UUID(parallel_leg_id)
        except (ValueError, TypeError):
            return {"status": "ignored", "reason": "invalid_parallel_leg_id"}
        answered_by = str(form_data.get("AnsweredBy") or "").strip().lower()
        machine_answers = {
            "machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax",
        }
        mapped_status = {
            "in-progress": "answered",
            "canceled": "cancelled",
        }.get(call_status, call_status)
        event_at = _twilio_event_time(form_data.get("Timestamp"))
        if mapped_status == "answered" and child_call_sid:
            _maybe_start_call_sorter(background_tasks, call_ref=child_call_sid, leg_call_sid=child_call_sid)
        if mapped_status == "answered" and event_at and child_call_sid:
            await dialer_parallel.record_parallel_timing(
                parent_call_sid=parent_call_sid,
                child_call_sid=child_call_sid,
                event_kind="answer",
                event_at=event_at,
                leg_id=parallel_leg_id,
            )
        if answered_by in machine_answers:
            # AMD classification and provider call completion are separate
            # signals. Persist a machine result first so it can disposition
            # the lead, and wait for the completed callback before treating
            # this line as finished.
            if call_status not in {"completed", "busy", "no-answer", "failed", "cancelled", "canceled"}:
                mapped_status = "machine"
            elif call_status == "completed":
                await dialer_parallel.update_parallel_leg(
                    child_call_sid=child_call_sid,
                    leg_id=parallel_leg_id,
                    status="machine",
                    answered_by=answered_by,
                )
                mapped_status = "completed"
        allowed_statuses = {
            "initiated", "ringing", "answered", "machine", "completed",
            "busy", "no-answer", "failed", "cancelled",
        }
        if mapped_status not in allowed_statuses:
            logger.warning(f"Ignoring unsupported parallel call status {call_status!r}")
            return {"status": "ignored", "reason": "unsupported_parallel_status"}
        await dialer_parallel.update_parallel_leg(
            child_call_sid=child_call_sid,
            leg_id=parallel_leg_id,
            status=mapped_status,
            answered_by=answered_by or None,
        )
        if child_call_sid and mapped_status in {"completed", "busy", "no-answer", "failed", "cancelled"}:
            try:
                await dialer_hold.record_ended(child_call_sid=child_call_sid,ended_at=event_at or datetime.now(UTC).isoformat())
                await dialer_agent_handoff.record_handoff_event(
                    call_id=child_call_sid,event="ended",event_at=event_at or datetime.now(UTC).isoformat(),
                    duration_seconds=duration_seconds,
                )
            except dialer_agent_handoff.AgentHandoffUnavailable:
                raise HTTPException(status_code=503, detail="Could not settle agent handoff history")
            await close_inbound_call(provider_call_id=child_call_sid)
            await evaluate_overflow_guard(provider_call_id=child_call_sid, provider="twilio")
            # An extra answer's person hung up: settle where the chain took them.
            await extra_answer.mark("twilio", child_call_sid, "ended")
            if mapped_status == "completed":
                # A machine line whose playback the carrier ended early: mailbox full or
                # cut short, never "voicemail left" (no-op when nothing was playing).
                await settle_interrupted_playback(
                    parent_call_sid=parent_call_sid, child_call_sid=child_call_sid,
                    leg_id=parallel_leg_id,
                )
        legs = await dialer_parallel.parallel_legs(parent_call_sid=parent_call_sid)
        session = await dialer_parallel.parallel_session(parent_call_sid=parent_call_sid)
        if (
            session
            and session.get("status") in {"completed", "failed", "cancelled"}
            and _parallel_batch_has_settled(legs)
        ):
            await dialer_parallel.apply_parallel_abandon_guard(
                parent_call_sid=parent_call_sid
            )
        if (
            session
            and not session.get("winning_child_call_sid")
            and _parallel_batch_has_settled(legs)
        ):
            await dialer_parallel.finish_parallel_session(
                parent_call_sid=parent_call_sid,
                status="failed",
            )
            await update_dialer_call_status(
                parent_call_sid=parent_call_sid,
                child_call_sid=None,
                status="failed",
                duration_seconds=duration_seconds or 0,
            )
            await cancel_call(call_sid=parent_call_sid)
        return {"status": "success"}

    await update_dialer_call_status(
        parent_call_sid=parent_call_sid,
        child_call_sid=child_call_sid,
        status=call_status,
        duration_seconds=duration_seconds,
    )
    await _record_single_call_status_stage(
        parent_call_sid=parent_call_sid, child_call_sid=child_call_sid, call_status=call_status,
    )
    if call_status == "in-progress":
        _maybe_start_call_sorter(background_tasks, call_ref=parent_call_sid, leg_call_sid=child_call_sid)
        if child_call_sid and await _leave_self_requested(parent_call_sid):
            background_tasks.add_task(
                _join_mailbox_to_rep_now, parent_call_sid=parent_call_sid, child_call_sid=child_call_sid,
            )
        if child_call_sid:
            background_tasks.add_task(
                _beep_trial_watch, parent_call_sid=parent_call_sid, child_call_sid=child_call_sid,
            )
    return {"status": "success"}


async def _record_single_call_status_stage(
    *, parent_call_sid: str, child_call_sid: str | None, call_status: str,
) -> None:
    """Stage evidence from the prospect leg's own status callback (single calls).

    ringing -> 'ringing'; rang out / busy -> 'no_answer'; and a playback the carrier
    ended within a few seconds of starting -> 'mailbox_full' (recorded as a failed
    voicemail, never as left). Fail-soft: evidence only.
    """
    try:
        if call_status == "ringing":
            await call_stages.record_call_stage(parent_call_sid, "ringing", {})
            return
        if call_status in {"no-answer", "busy"}:
            await call_stages.record_call_stage(parent_call_sid, "no_answer", {"status": call_status})
        if call_status not in {"completed", "no-answer", "busy", "failed", "canceled"}:
            return
        row = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)
        if not row or (child_call_sid and row.get("child_call_sid") != child_call_sid):
            return
        if row.get("manual_voicemail_requested_at") and child_call_sid:
            # A ring-back offer for this person ends with their call, and so does the
            # extra-answer chain that may have followed it.
            await close_inbound_call(provider_call_id=child_call_sid)
            await extra_answer.mark("twilio", child_call_sid, "ended")
        started = row.get("voicemail_drop_started_at")
        if call_status == "completed" and started and row.get("auto_voicemail_sent") is not True:
            # The carrier ended the call while the recording was still playing (the
            # Redirect after <Play> never ran). Measured against the recording's length:
            # mailbox full, cut short, or left. Always a final outcome.
            await settle_interrupted_playback(
                parent_call_sid=parent_call_sid, child_call_sid=child_call_sid,
            )
    except Exception as exc:  # noqa: BLE001 - status webhook must not fail on evidence
        logger.info(f"Could not record status stage for {parent_call_sid}: {type(exc).__name__}")


@router.post("/dialer-recording-callback", include_in_schema=False)
async def handle_dialer_recording_callback(request: Request):
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-recording-callback webhook")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # A Conference recording's RecordingStatusCallback identifies the
    # conference via ConferenceSid, not any participant's CallSid - this
    # replaced the old <Dial record> callback shape (which reported the
    # parent leg's own CallSid) when calls moved to a named Conference for
    # live-monitoring support. See
    # docs/superpowers/specs/2026-08-12-dialer-live-call-monitoring-design.md
    # in the sysevo repo.
    conference_sid = form_data.get("ConferenceSid", "")
    recording_sid = form_data.get("RecordingSid", "")
    recording_status = form_data.get("RecordingStatus", "")
    if not conference_sid or not recording_sid:
        logger.warning("dialer-recording-callback missing ConferenceSid or RecordingSid")
        return {"status": "ignored", "reason": "missing_fields"}

    # <Conference record="record-from-start"> fires this webhook only on
    # completion by default - but that's the TwiML's behavior, not this
    # handler's guarantee. If recordingStatusCallbackEvent is ever expanded
    # to include in-progress/absent events, this guard stops a premature or
    # failed recording from being written as if it were a playable
    # completed one. Logged rather than dropped in silence: of the statuses
    # that would then arrive (in-progress/completed/absent/failed), "failed"
    # means the operator expects audio and none exists, which is exactly the
    # kind of thing that otherwise only surfaces as a human noticing.
    if recording_status != "completed":
        logger.info(
            f"dialer-recording-callback ignoring recording {recording_sid} for conference "
            f"{conference_sid} at status '{recording_status or 'unknown'}' - not completed"
        )
        return {"status": "ignored", "reason": "recording_not_completed"}

    matched = await update_dialer_call_recording_by_conference_sid(
        conference_sid=conference_sid, recording_sid=recording_sid
    )
    if matched is False:
        # conference-start never reached us (or its write failed), so no row
        # carries this conference_sid. The conference is named after the rep's
        # call: ask Twilio for that name and attach the recording by it, so an
        # answered, recorded call never ends up with no audio or transcript.
        parent_call_sid = await _parent_call_sid_for_conference(conference_sid)
        if parent_call_sid and await update_dialer_call_recording_by_parent_call_sid(
            parent_call_sid=parent_call_sid,
            conference_sid=conference_sid,
            recording_sid=recording_sid,
        ):
            logger.info(
                f"dialer-recording-callback attached orphaned recording {recording_sid} "
                f"to {parent_call_sid} by conference name"
            )
        else:
            logger.error(
                f"dialer-recording-callback could not attach recording {recording_sid} "
                f"(conference {conference_sid}) to any call"
            )
    return {"status": "success"}


_REP_LEG_ENDED_STATUSES = {"completed", "no-answer", "busy", "failed", "canceled"}
_REP_WATCH_INTERVAL_SECONDS = 3
_rep_watch_sleep = asyncio.sleep


async def _cancel_lead_if_rep_leaves(
    *, parent_call_sid: str, child_call_sid: str, ring_seconds: int,
) -> None:
    """While the lead's phone rings, cancel it as soon as the rep's own leg has
    ended. A rep who handed the line to a queued voicemail (rep_detached_at) is
    left alone: that lead must keep ringing for the mailbox. Only a lead still
    ringing is cancelled, never one that answered."""
    window = max(15, int(ring_seconds or 30)) + 15
    for _ in range(window // _REP_WATCH_INTERVAL_SECONDS):
        await _rep_watch_sleep(_REP_WATCH_INTERVAL_SECONDS)
        if not await _rep_leg_ended(parent_call_sid):
            continue
        evidence = await get_voicemail_drop_evidence(parent_call_sid=parent_call_sid)
        if evidence.get("rep_detached_at"):
            return
        if await cancel_call_if_ringing(call_sid=child_call_sid):
            logger.info(
                f"Rep left {parent_call_sid} while {child_call_sid} rang: cancelled the lead leg"
            )
        return



async def _rep_leg_ended(parent_call_sid: str) -> bool:
    """Whether the rep's own (browser) leg has already ended, per Twilio.
    Any doubt (no client, lookup failed) answers False: the prospect is then
    joined exactly as before, never hung up on a guess."""
    client = await _twilio_client()
    if not client:
        return False
    try:
        call = await asyncio.to_thread(client.calls(parent_call_sid).fetch)
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not fail the join
        logger.warning(f"Could not read the rep leg {parent_call_sid}: {exc}")
        return False
    return str(getattr(call, "status", "") or "").lower() in _REP_LEG_ENDED_STATUSES


async def _parent_call_sid_for_conference(conference_sid: str) -> str | None:
    """The rep's call SID from the conference's own name (call-<parent>)."""
    client = await _twilio_client()
    if not client:
        return None
    try:
        conference = await asyncio.to_thread(client.conferences(conference_sid).fetch)
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not fail the webhook
        logger.error(f"Could not fetch conference {conference_sid}: {exc}")
        return None
    parent = parent_call_sid_from_conference_name(getattr(conference, "friendly_name", "") or "")
    return parent if parent and _TWILIO_CALL_SID_PATTERN.fullmatch(parent) else None


@router.post("/twiml", include_in_schema=False)
async def handle_twiml_webhook(
    workflow_id: int,
    user_id: int,
    workflow_run_id: int,
    organization_id: int,
    request: Request,
):
    """
    Handle initial webhook from telephony provider.
    Returns provider-specific response (e.g., TwiML for Twilio).
    Never returns a non-200 response — Twilio plays an error message if we do.
    """
    _hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    try:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        if not workflow_run:
            logger.error(f"[run {workflow_run_id}] Workflow run not found for TwiML webhook")
            return HTMLResponse(content=_hangup, media_type="application/xml")

        provider = await get_telephony_provider_for_run(workflow_run, organization_id)
        callback_data = dict(await request.form())

        is_valid = await provider.verify_inbound_signature(
            str(request.url),
            callback_data,
            dict(request.headers),
        )
        if not is_valid:
            logger.warning(
                f"[run {workflow_run_id}] Invalid Twilio signature on answer webhook"
            )
            return HTMLResponse(content=_hangup, media_type="application/xml")

        response_content = await provider.get_webhook_response(
            workflow_id, user_id, workflow_run_id
        )
        return HTMLResponse(content=response_content, media_type="application/xml")

    except Exception as exc:
        logger.error(f"[run {workflow_run_id}] TwiML webhook error: {exc}")
        return HTMLResponse(content=_hangup, media_type="application/xml")


@router.post("/twilio/status-callback/{workflow_run_id}")
async def handle_twilio_status_callback(
    workflow_run_id: int,
    request: Request,
):
    """Handle Twilio-specific status callbacks."""
    set_current_run_id(workflow_run_id)

    # Parse form data
    form_data = await request.form()
    callback_data = dict(form_data)

    logger.info(
        f"[run {workflow_run_id}] Received status callback: {json.dumps(callback_data)}"
    )

    # Get workflow run to find organization
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(f"Workflow run {workflow_run_id} not found for status callback")
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    # Get workflow and provider
    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"Workflow {workflow_run.workflow_id} not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    is_valid = await provider.verify_inbound_signature(
        str(request.url),
        callback_data,
        dict(request.headers),
    )
    if not is_valid:
        logger.warning(f"Invalid webhook signature for workflow run {workflow_run_id}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Parse the callback data into generic format
    parsed_data = provider.parse_status_callback(callback_data)

    # Create StatusCallbackRequest from parsed data
    status_update = StatusCallbackRequest(
        call_id=parsed_data["call_id"],
        status=parsed_data["status"],
        from_number=parsed_data.get("from_number"),
        to_number=parsed_data.get("to_number"),
        direction=parsed_data.get("direction"),
        duration=parsed_data.get("duration"),
        extra=parsed_data.get("extra", {}),
    )

    # Process the status update
    await _process_status_update(workflow_run_id, status_update)

    return {"status": "success"}


# In-call controls (hold, recording, keypad tones, transfer, add call) live in their
# own module and are mounted here so they share this router's /telephony prefix.
from api.services.telephony.providers.twilio.dialer_call_controls import router as _call_controls_router  # noqa: E402
router.include_router(_call_controls_router)
