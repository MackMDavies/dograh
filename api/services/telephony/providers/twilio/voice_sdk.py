"""Twilio Voice SDK access tokens for browser-based (softphone) calling.

Mints short-lived Access Tokens with a VoiceGrant so a sales rep's browser
can register as a Twilio.Device and place calls through a TwiML App. Uses
the active platform Twilio account's dialer credentials (same account as
managed_provisioner.py resolves for provisioning), not the per-org
TelephonyConfigurationModel — this is intentionally internal-only for now.

This uses only the independent sales-dialer Twilio account. It does not
fall back to AI Voice Managed Telephony credentials or shared environment
variables.
"""

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

TOKEN_TTL_SECONDS = 3600


class VoiceSdkNotConfigured(Exception):
    """Raised when no source (DB or env) has complete Voice SDK credentials."""


async def _resolve_dialer_credentials() -> dict | None:
    from api.db import db_client

    try:
        return await db_client.get_platform_dialer_credentials()
    except Exception:  # noqa: BLE001 — never fall back to another Twilio account
        return None


async def generate_voice_access_token(identity: str) -> str:
    creds = await _resolve_dialer_credentials()
    if not creds:
        raise VoiceSdkNotConfigured(
            "The independent sales dialer Twilio connection is not configured. "
            "Set it in Dialer > Providers > Twilio."
        )

    token = AccessToken(
        creds["account_sid"],
        creds["api_key_sid"],
        creds["api_key_secret"],
        identity=identity,
        ttl=TOKEN_TTL_SECONDS,
    )
    token.add_grant(
        VoiceGrant(outgoing_application_sid=creds["twiml_app_sid"], incoming_allow=False)
    )

    jwt_str = token.to_jwt()
    if isinstance(jwt_str, bytes):
        jwt_str = jwt_str.decode("utf-8")
    return jwt_str
