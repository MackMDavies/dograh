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
