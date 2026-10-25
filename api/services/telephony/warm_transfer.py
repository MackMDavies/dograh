"""Warm transfers: the colleague hears who is calling before they are connected.

When a transfer tool is set to warm, the agent writes a short introduction ("Jane Doe
is calling about her boiler service on Friday"). When the colleague answers, the
carrier reads it to them and asks them to press 1 to take the call. Only a press of 1
connects the caller. Anything else (no press, another key, a hang-up, voicemail
picking up) counts as not answered, so the agent can try the backup number or tell
the caller nobody is free, instead of handing them to a voicemail greeting.

Carriers that cannot read text to the colleague keep the cold transfer.
"""

from __future__ import annotations

from typing import Optional
from xml.sax.saxutils import escape

ACCEPT_KEY = "1"
ACCEPT_TIMEOUT_SECONDS = 8
MAX_INTRODUCTION_CHARS = 400

ACCEPT_PROMPT = "Press 1 to take the call."
DECLINED_GOODBYE = "Okay, the caller will not be connected. Goodbye."


def clean_introduction(text: object) -> Optional[str]:
    words = " ".join(str(text or "").split())[:MAX_INTRODUCTION_CHARS].strip()
    return words or None


def briefing_speech(introduction: str) -> str:
    """What the colleague hears when they answer."""
    intro = introduction.rstrip()
    if intro and intro[-1] not in ".!?":
        intro += "."
    return f"You have a call transfer. {intro} {ACCEPT_PROMPT}"


def twilio_transfer_twiml(
    conference_name: str,
    introduction: Optional[str] = None,
    accept_url: Optional[str] = None,
) -> str:
    """TwiML for the colleague's leg. Cold: straight into the conference."""
    conference = escape(conference_name)
    if not introduction or not accept_url:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>You have answered a transfer call. Connecting you now.</Say>
    <Dial>
        <Conference endConferenceOnExit="true">{conference}</Conference>
    </Dial>
</Response>"""
    url = escape(accept_url)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="dtmf" numDigits="1" timeout="{ACCEPT_TIMEOUT_SECONDS}" action="{url}" method="POST">
        <Say>{escape(briefing_speech(introduction))}</Say>
    </Gather>
    <Redirect method="POST">{url}</Redirect>
</Response>"""


def twilio_accept_twiml(conference_name: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you now.</Say>
    <Dial>
        <Conference endConferenceOnExit="true">{escape(conference_name)}</Conference>
    </Dial>
</Response>"""


def twilio_decline_twiml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{DECLINED_GOODBYE}</Say>
    <Hangup/>
</Response>"""


def masked_destination(destination: str) -> str:
    """Enough of a number for the call history to tell destinations apart."""
    digits = "".join(c for c in destination if c.isdigit())
    if destination.upper().startswith(("SIP/", "PJSIP/")) or len(digits) < 4:
        return destination.split("@")[0]
    return f"…{digits[-4:]}"


# ── Carrier-neutral outcome reporting (Vonage, Plivo, Vobiz webhooks) ─────────────
# Twilio and Telnyx have their own routes, written before these helpers; the rules
# are the same: a warm leg answering is not taking the call, only a press of 1 is;
# every transfer reports exactly one outcome.

LEG_OUTCOMES = ("answered", "no_answer", "busy", "failed", "completed")


async def _publish(manager, context, *, connected: bool, reason: Optional[str], leg_id: str) -> None:
    from api.services.telephony.transfer_event_protocol import TransferEvent, TransferEventType

    await manager.publish_transfer_event(
        TransferEvent(
            type=TransferEventType.DESTINATION_ANSWERED if connected else TransferEventType.TRANSFER_FAILED,
            transfer_id=context.transfer_id,
            original_call_sid=context.original_call_sid or "",
            transfer_call_sid=leg_id,
            conference_name=context.conference_name,
            status="success" if connected else "transfer_failed",
            action="destination_answered" if connected else "transfer_failed",
            reason=None if connected else reason,
            message="The person took the call." if connected else f"Transfer did not connect ({reason}).",
            end_call=not connected,
        )
    )


async def report_transfer_leg(transfer_id: str, outcome: str, leg_id: str = "") -> str:
    """A carrier status event for the colleague's leg. Returns what was done."""
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager

    manager = await get_call_transfer_manager()
    context = await manager.get_transfer_context(transfer_id)
    if context is None or context.resolved or outcome not in LEG_OUTCOMES:
        return "ignored"
    if outcome == "answered":
        if context.warm:
            context.answered = True
            await manager.store_transfer_context(context)
            return "pending"
        context.resolved = context.accepted = True
        await manager.store_transfer_context(context)
        await _publish(manager, context, connected=True, reason=None, leg_id=leg_id)
        return "connected"
    if outcome == "completed":
        if not context.warm:
            return "ignored"  # a cold leg's hang-up after it connected is not news
        outcome = "declined" if context.answered else "no_answer"
    context.resolved = True
    await manager.store_transfer_context(context)
    await _publish(manager, context, connected=False, reason=outcome, leg_id=leg_id)
    return outcome


async def report_transfer_accept(transfer_id: str, digits: str, leg_id: str = "") -> Optional[str]:
    """The colleague's key press on a warm leg: the conference name if they took the
    call (the carrier joins them to it), None if they did not."""
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager

    manager = await get_call_transfer_manager()
    context = await manager.get_transfer_context(transfer_id)
    if context is None or context.resolved:
        return None
    accepted = str(digits or "").startswith(ACCEPT_KEY)
    context.resolved = True
    context.accepted = accepted
    await manager.store_transfer_context(context)
    await _publish(manager, context, connected=accepted, reason=None if accepted else "declined", leg_id=leg_id)
    return context.conference_name if accepted else None


def vonage_conversation_ncco(conference_name: str) -> list[dict]:
    """NCCO joining a leg to the transfer's conversation; it ends when the colleague leaves."""
    return [{"action": "conversation", "name": conference_name, "endOnExit": True}]
