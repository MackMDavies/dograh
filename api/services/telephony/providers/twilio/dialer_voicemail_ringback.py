"""Twilio ring-back: a person answered after the rep pressed "Send voicemail".

The rep already left for the next lead, so the person is never put in the original
(now empty) conference and never hears the recording. Instead the ORIGINAL rep is
offered the call through the ordinary inbound-offer path (an ``inbound_calls`` row
targeting only them, pushed to their softphone). The person waits in a private
conference named after their own call (``call-<prospect SID>``), which the rep's
browser joins through voice-connect's claimed-conference branch, exactly like a Hold
offer. If the rep is busy or offline, or does not take it within the offer window,
today's behaviour applies: the line ends and the call is recorded as
``person_answered`` ("picked up after you moved on — call them back").
"""

import asyncio
from xml.sax.saxutils import escape

from loguru import logger

from api.services.telephony.dialer import call_stages
from api.services.telephony.dialer.inbound_call_log import (
    expire_unanswered_inbound_call,
    notify_inbound_call,
)
from api.services.telephony.providers.twilio.dialer_call_log import (
    record_voicemail_playback_failure,
)
from api.services.telephony.providers.twilio.dialer_conference import (
    cancel_call,
    conference_name_for,
)
from api.services.telephony.providers.twilio.dialer_twiml import conference_telemetry_attributes


async def offer_ringback_twiml(
    *, parent_call_sid: str, prospect_call_sid: str, endpoint_root: str,
) -> str | None:
    """TwiML that parks the person for their original rep, or None (rep not free)."""
    conference_name = conference_name_for(prospect_call_sid)
    offer = await call_stages.offer_voicemail_ringback(
        provider="twilio", parent_call_sid=parent_call_sid,
        prospect_call_id=prospect_call_sid, conference_name=conference_name,
    )
    if not offer:
        return None
    notify_inbound_call(str(offer["inbound_call_id"]))
    attrs = conference_telemetry_attributes(
        events_url=f"{endpoint_root}/dialer-conference-events",
        recording_url=f"{endpoint_root}/dialer-recording-callback",
    )
    # The person waits (Twilio's hold music) until the rep's leg starts the room.
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        "<Say>This call may be recorded and monitored for quality assurance.</Say>"
        "<Dial><Conference " + attrs
        + 'startConferenceOnEnter="false" endConferenceOnExit="true" beep="false">'
        + escape(conference_name) + "</Conference></Dial></Response>"
    )


async def expire_ringback(
    *, parent_call_sid: str, prospect_call_sid: str,
    delay_seconds: float = call_stages.RINGBACK_OFFER_SECONDS,
) -> None:
    """Let the person go if the rep never took the ring-back (atomic with a late claim)."""
    await asyncio.sleep(delay_seconds)
    try:
        if not await expire_unanswered_inbound_call(provider_call_id=prospect_call_sid, provider="twilio"):
            return  # the rep took it (or the person already hung up and it was closed)
        await cancel_call(call_sid=prospect_call_sid)
        await record_voicemail_playback_failure(parent_call_sid=parent_call_sid, reason="person_answered")
        await call_stages.record_call_stage(
            parent_call_sid, "person", {"after_vm_request": True, "ringback": "missed"},
        )
    except Exception as exc:  # noqa: BLE001 - background timer must not raise
        logger.error(f"Twilio ring-back expiry failed for {parent_call_sid}: {exc}")
