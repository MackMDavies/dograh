"""Twilio Conference helpers for live call monitoring.

Every dialer call is a named Conference ("call-{parent_call_sid}") instead
of a plain <Dial><Number> bridge, specifically so a manager can join
mid-call as a muted listener - Twilio has no way to inject a third
participant into an already-bridged two-party <Dial> call. See
docs/superpowers/specs/2026-08-12-dialer-live-call-monitoring-design.md
in the sysevo repo.
"""

import asyncio
import os

from loguru import logger
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client


def conference_name_for(parent_call_sid: str) -> str:
    return f"call-{parent_call_sid}"


def parent_call_sid_from_conference_name(name: str) -> str | None:
    if not name.startswith("call-"):
        return None
    return name.removeprefix("call-")


def _twilio_client() -> Client | None:
    account_sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return None
    # An explicit timeout is required, not tidiness: twilio-python's default
    # http client passes timeout=None to requests, i.e. wait forever. Both
    # callers here run inside a Twilio webhook handler and inside an
    # asyncio.to_thread worker, so a stalled Twilio API call would blow past
    # Twilio's own 15s webhook timeout (earning a retry, and a duplicate
    # side effect with it) while pinning a thread from a pool that is only
    # ~8 threads wide on the 4-core prod box - starving dial_lead_into_
    # conference, which is on the critical path of every single dial.
    # 5s matches the timeout every Supabase call in dialer_call_log.py uses.
    return Client(account_sid, auth_token, http_client=TwilioHttpClient(timeout=5))


async def dial_lead_into_conference(
    *,
    parent_call_sid: str,
    lead_number: str,
    caller_id: str,
    join_conference_url: str,
    status_callback_url: str,
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
    client = _twilio_client()
    if not client:
        logger.error("dial_lead_into_conference: Twilio credentials not configured")
        return None
    try:
        call = await asyncio.to_thread(
            client.calls.create,
            to=lead_number,
            from_=caller_id,
            url=join_conference_url,
            method="POST",
            status_callback=status_callback_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
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
    client = _twilio_client()
    if not client:
        logger.error("cancel_call: Twilio credentials not configured")
        return False
    try:
        await asyncio.to_thread(client.calls(call_sid).update, status="completed")
        return True
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into a webhook handler
        logger.error(f"Failed to cancel call {call_sid}: {exc}")
        return False
