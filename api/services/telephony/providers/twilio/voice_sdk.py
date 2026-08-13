"""Twilio Voice SDK access tokens for browser-based (softphone) calling.

Mints short-lived Access Tokens with a VoiceGrant so a sales rep's browser
can register as a Twilio.Device and place calls through a TwiML App. Uses
the active platform Twilio account's dialer credentials (same account as
managed_provisioner.py resolves for provisioning), not the per-org
TelephonyConfigurationModel — this is intentionally internal-only for now.

Resolution order: DB-stored dialer credentials on the active platform
account first, then the SYSEVO_TWILIO_ACCOUNT_SID / TWILIO_API_KEY_SID /
TWILIO_API_KEY_SECRET / TWILIO_TWIML_APP_SID env vars as a fallback. An
account can have valid account_sid/auth_token (enough for provisioning)
without dialer fields set — those three (API Key pair + TwiML App) must be
created in that Twilio account's own console first; there's no API-only way
to provision them, so switching the active account doesn't automatically
make the dialer work on it until an admin fills those fields in too.
"""

import os

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

TOKEN_TTL_SECONDS = 3600


class VoiceSdkNotConfigured(Exception):
    """Raised when no source (DB or env) has complete Voice SDK credentials."""


async def _resolve_dialer_credentials() -> dict | None:
    from api.db import db_client

    try:
        creds = await db_client.get_platform_dialer_credentials()
        if creds and creds.get("twiml_app_sid"):
            return creds
    except Exception:  # noqa: BLE001 — never block token issuance on a DB hiccup
        pass

    account_sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
    api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    twiml_app_sid = os.environ.get("TWILIO_TWIML_APP_SID")
    if all([account_sid, api_key_sid, api_key_secret, twiml_app_sid]):
        return {
            "account_sid": account_sid,
            "api_key_sid": api_key_sid,
            "api_key_secret": api_key_secret,
            "twiml_app_sid": twiml_app_sid,
        }
    return None


async def generate_voice_access_token(identity: str) -> str:
    creds = await _resolve_dialer_credentials()
    if not creds:
        raise VoiceSdkNotConfigured(
            "No Twilio account has dialer credentials configured (API Key pair + "
            "TwiML App SID), and the SYSEVO_TWILIO_ACCOUNT_SID / TWILIO_API_KEY_SID / "
            "TWILIO_API_KEY_SECRET / TWILIO_TWIML_APP_SID env vars are not fully set."
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
