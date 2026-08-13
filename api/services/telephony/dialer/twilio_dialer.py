"""Twilio implementation of the dialer provider.

Delegates to the existing voice_sdk token minting - this is a re-home behind
an interface, not a rewrite. Behaviour must stay identical so that flipping
the active provider back to Twilio restores exactly today's behaviour.
"""

from api.services.telephony.dialer.provider import DialerCredentials
from api.services.telephony.providers.twilio.voice_sdk import (
    generate_voice_access_token,
)


class TwilioDialerProvider:
    name = "twilio"

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        identity = f"rep-{user_id}"
        token = generate_voice_access_token(identity)
        # Twilio routes by the TwiML App's fixed Voice URL, so the browser has
        # no destination to dial - empty string keeps one response shape.
        return DialerCredentials(token=token, identity=identity, destination="")
