"""Twilio Voice SDK access tokens for browser-based (softphone) calling.

Mints short-lived Access Tokens with a VoiceGrant so a sales rep's browser
can register as a Twilio.Device and place calls through the TwiML App
configured at TWILIO_TWIML_APP_SID. Uses the Sysevo platform Twilio account
(same credentials as managed_provisioner.py), not the per-org
TelephonyConfigurationModel — this is intentionally internal-only for now.
"""

import os

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

TOKEN_TTL_SECONDS = 3600


class VoiceSdkNotConfigured(Exception):
    """Raised when the Twilio Voice SDK env vars are not fully set."""


def generate_voice_access_token(identity: str) -> str:
    account_sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
    api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    twiml_app_sid = os.environ.get("TWILIO_TWIML_APP_SID")

    if not all([account_sid, api_key_sid, api_key_secret, twiml_app_sid]):
        raise VoiceSdkNotConfigured(
            "SYSEVO_TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, "
            "and TWILIO_TWIML_APP_SID must all be set to issue Voice SDK tokens."
        )

    token = AccessToken(
        account_sid,
        api_key_sid,
        api_key_secret,
        identity=identity,
        ttl=TOKEN_TTL_SECONDS,
    )
    token.add_grant(
        VoiceGrant(outgoing_application_sid=twiml_app_sid, incoming_allow=False)
    )

    jwt_str = token.to_jwt()
    if isinstance(jwt_str, bytes):
        jwt_str = jwt_str.decode("utf-8")
    return jwt_str
