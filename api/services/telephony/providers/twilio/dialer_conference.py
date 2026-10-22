"""Twilio Conference helpers for live call monitoring.

Every dialer call is a named Conference ("call-{parent_call_sid}") instead
of a plain <Dial><Number> bridge, specifically so a manager can join
mid-call as a muted listener - Twilio has no way to inject a third
participant into an already-bridged two-party <Dial> call. See
docs/superpowers/specs/2026-08-12-dialer-live-call-monitoring-design.md
in the sysevo repo.
"""

import asyncio

from loguru import logger
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client


def conference_name_for(parent_call_sid: str) -> str:
    return f"call-{parent_call_sid}"


def parent_call_sid_from_conference_name(name: str) -> str | None:
    if not name.startswith("call-"):
        return None
    return name.removeprefix("call-")


async def _twilio_client() -> Client | None:
    from api.db import db_client

    try:
        credentials = await db_client.get_platform_dialer_credentials()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Dialer Twilio credential lookup failed: {exc}")
        return None
    if not credentials:
        return None
    # Explicit timeout: this is on the critical path of every outbound dial.
    return Client(credentials["account_sid"], credentials["auth_token"], http_client=TwilioHttpClient(timeout=5))

async def dial_lead_into_conference(
    *,
    parent_call_sid: str,
    lead_number: str,
    caller_id: str,
    join_conference_url: str,
    status_callback_url: str,
    machine_detection: bool = False,
    machine_detection_preset: str = "balanced",
    ringing_timeout_seconds: int | None = None,
    recording_callback_url: str | None = None,
) -> str | None:
    """Places the outbound leg to the lead, joining them into the same
    conference as the rep, with status tracking attached the same way the
    old <Number statusCallback> used to work - just via the REST API's own
    status_callback params instead of embedded TwiML, since there's no
    <Number> noun anymore. Returns the new call's SID, or None on failure -
    fails soft, must never break the rep's already-connected call.

    Twilio's Python SDK is synchronous - client.calls.create() is a
    blocking HTTP call, so it's wrapped in asyncio.to_thread() to avoid
    blocking the FastAPI event loop (and every other concurrent request)
    for the time it takes Twilio to respond.
    """
    client = await _twilio_client()
    if not client:
        logger.error("dial_lead_into_conference: Twilio credentials not configured")
        return None
    try:
        params = {
            "to": lead_number,
            "from_": caller_id,
            "url": join_conference_url,
            "method": "POST",
            "status_callback": status_callback_url,
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "status_callback_method": "POST",
        }
        if recording_callback_url:
            # Record the lead's own leg from the moment it answers. The conference
            # recording only exists once two people are in the room, so a voicemail
            # played into a mailbox, a greeting the rep left a live message on, or a
            # line that hung up before joining had no recording at all (22 Oct:
            # "too many calls have no recording"). The callback attaches it to the
            # call only when the call has no recording yet.
            params["record"] = True
            params["recording_status_callback"] = recording_callback_url
            params["recording_status_callback_event"] = ["completed"]
            params["recording_channels"] = "mono"
        if ringing_timeout_seconds is not None:
            # Bound each destination in a parallel batch so an empty round
            # cannot leave its parent conference open indefinitely.
            params["timeout"] = max(5, min(60, int(ringing_timeout_seconds)))
        if machine_detection:
            # DetectMessageEnd holds the lead leg until a greeting ends. The
            # join webhook then decides whether to play the saved message.
            params["machine_detection"] = "DetectMessageEnd"
            # Twilio exposes speech thresholds and an overall timeout rather
            # than a single "speed" control. Keep all values within its API
            # bounds; the preset influences classification latency/accuracy,
            # while DetectMessageEnd still waits for a machine greeting.
            # MachineDetectionTimeout is how long Twilio may hold the line looking
            # for the end of a greeting. When it runs out on a mailbox (no beep it
            # recognises) the result is machine_end_other and the mailbox has been
            # recording silence the whole time, so a long limit loses messages:
            # 30s on "balanced" did exactly that in production. A limit shorter than
            # a typical greeting (3s on "fastest") plays over the greeting instead.
            # 2026-10-21: 28 of 31 mailboxes in 30 days ran to the limit (beeps that
            # were detected came at 12-20s) and ~1 in 5 hung up on the silence before
            # the message ended, so "balanced" waits 16s, not 20s (owner decision).
            amd_presets = {
                "fastest": (8, 1000, 500, 2000),
                "fast": (12, 1800, 900, 3000),
                "balanced": (16, 2400, 1200, 5000),
                "accurate": (30, 3200, 2000, 7000),
            }
            timeout, speech_threshold, speech_end_threshold, silence_timeout = amd_presets.get(
                machine_detection_preset, amd_presets["balanced"]
            )
            params.update({
                "machine_detection_timeout": timeout,
                "machine_detection_speech_threshold": speech_threshold,
                "machine_detection_speech_end_threshold": speech_end_threshold,
                "machine_detection_silence_timeout": silence_timeout,
            })
        call = await asyncio.to_thread(client.calls.create, **params)
        return call.sid
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into voice-connect
        logger.error(f"Failed to dial lead into conference for {parent_call_sid}: {exc}")
        return None


async def cancel_call(*, call_sid: str) -> bool:
    """End an outbound call we no longer want connected, whatever
    non-terminal state it's in.

    The rep's leg carries endConferenceOnExit="true", so the rep hanging up
    (or skipping on to the next lead) ends the conference - but ending a
    conference only drops its *participants*. A lead whose phone is still
    ringing has not joined yet: their call is an independent in-flight leg
    whose TwiML hasn't run, so it survives, they answer, and they land in a
    freshly-recreated empty conference and hear silence. That is an
    abandoned call placed from our own caller ID - FCC/FTC territory, not
    just bad UX. The old <Dial><Number> bridge cancelled that leg for us;
    with Conference we have to do it ourselves.

    Sends Status=completed rather than Status=canceled even though this is
    semantically a cancel: "canceled" is only valid for a queued or ringing
    call, and there is an irreducible window between the rep hanging up and
    this landing (conference-end delivery latency + a Supabase read + this
    REST call). A lead who answers inside that window is in-progress, a
    "canceled" would be rejected, and they'd be left alone in a silent
    recreated conference - precisely the abandoned call this exists to
    prevent, on the timing where it's most likely. "completed" terminates a
    call in any non-terminal state, ringing and in-progress alike.

    CAUTION for future callers: that choice gives up a safety net this
    function used to have for free. "canceled" was rejected by Twilio on any
    answered call, so a wrong or stale SID could only ever fail harmlessly;
    "completed" will genuinely terminate whatever live call the SID names.
    The name still reads safer than the behavior - only pass a SID you are
    certain belongs to a call you are entitled to end.

    Returns True if Twilio accepted it. Fails soft like everything else
    here - neither a rejection nor an outage may turn a status-callback
    webhook into a 500 that Twilio then retries.

    Twilio's Python SDK is synchronous, so this is wrapped in
    asyncio.to_thread() for the same reason dial_lead_into_conference is.
    """
    client = await _twilio_client()
    if not client:
        logger.error("cancel_call: Twilio credentials not configured")
        return False
    try:
        await asyncio.to_thread(client.calls(call_sid).update, status="completed")
        return True
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into a webhook handler
        logger.error(f"Failed to cancel call {call_sid}: {exc}")
        return False


async def cancel_call_if_ringing(*, call_sid: str) -> bool:
    """Cancel a parallel sibling only while Twilio still reports it ringing.

    Unlike ``cancel_call``, this must never end a second answered human: that
    call may be routed to an overflow teammate. Twilio validates the transition
    too, so a call that answers between fetch and update cannot be cancelled.
    """
    client = await _twilio_client()
    if not client:
        logger.error("cancel_call_if_ringing: Twilio credentials not configured")
        return False
    try:
        call = await asyncio.to_thread(client.calls(call_sid).fetch)
        if str(getattr(call, "status", "")).lower() not in {"queued", "ringing"}:
            return False
        await asyncio.to_thread(client.calls(call_sid).update, status="canceled")
        return True
    except Exception as exc:  # noqa: BLE001 - webhook cleanup must fail soft
        logger.info(f"Parallel sibling {call_sid} was no longer cancellable: {exc}")
        return False
