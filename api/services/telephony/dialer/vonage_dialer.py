"""Vonage Client SDK credentials for the sales-rep browser dialer.

This is deliberately separate from an organisation's AI telephony config.
The Client SDK JWT is generated with platform credentials and grants access to
the Vonage Client SDK APIs only.
"""

import os
import time
from uuid import uuid4

import jwt
import httpx

from api.services.telephony.dialer.provider import DialerCredentials


_CLIENT_SDK_ACL = {
    "paths": {
        "/*/users/**": {},
        "/*/rtc/**": {},
        "/*/conversations/**": {},
        "/*/sessions/**": {},
        "/*/devices/**": {},
        "/*/image/**": {},
        "/*/media/**": {},
        "/*/push/**": {},
        "/*/knocking/**": {},
        "/*/legs/**": {},
        "/*/calls/**": {},
    }
}


class VonageDialerNotConfigured(Exception):
    """Raised if platform Client SDK credentials are incomplete."""


class VonageDialerProvider:
    name = "vonage"

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        try:
            from api.db import db_client

            saved = await db_client.get_platform_vonage_dialer_credentials()
        except Exception as exc:  # noqa: BLE001 - don't fall back on DB failures
            raise VonageDialerNotConfigured("Could not verify saved Vonage dialer settings.") from exc
        saved = saved or {}
        application_id = (saved.get("application_id") or os.getenv("VONAGE_DIALER_APPLICATION_ID") or "").strip()
        api_key = (saved.get("api_key") or os.getenv("VONAGE_DIALER_API_KEY") or "").strip()
        private_key = saved.get("private_key") or os.getenv("VONAGE_DIALER_PRIVATE_KEY") or ""
        if not application_id or not api_key or not private_key:
            raise VonageDialerNotConfigured(
                "Vonage dialer is not configured. Set VONAGE_DIALER_APPLICATION_ID, "
                "VONAGE_DIALER_API_KEY, and VONAGE_DIALER_PRIVATE_KEY for the Voice application."
            )

        identity = f"rep-{user_id}"
        now = int(time.time())
        token = jwt.encode(
            {
                "application_id": application_id,
                "iat": now,
                "jti": str(uuid4()),
                "nbf": now,
                "exp": now + 3600,
                "sub": identity,
                "acl": _CLIENT_SDK_ACL,
            },
            private_key.replace("\\n", "\n"),
            algorithm="RS256",
        )
        # Vonage Client SDK identities must exist as Vonage Users before a
        # browser can establish its session. Creating is idempotent by unique
        # name: 409 means this rep was already provisioned.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.nexmo.com/v1/users",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"name": identity, "display_name": f"Sysevo rep {user_id}"},
                )
            if response.status_code not in (200, 201, 409):
                raise VonageDialerNotConfigured(
                    f"Vonage could not prepare the rep's Client SDK user (HTTP {response.status_code})."
                )
        except VonageDialerNotConfigured:
            raise
        except httpx.RequestError as exc:
            raise VonageDialerNotConfigured("Could not reach Vonage to prepare the dialer user.") from exc
        return DialerCredentials(
            token=token,
            identity=identity,
            destination="",
            caller_number=(saved.get("caller_number") or ""),
        )
