"""Telnyx WebRTC implementation for the Sysevo sales-rep dialer.

The browser uses Telnyx's WebRTC SDK and a short-lived JWT minted for an
on-demand telephony credential. Permanent Telnyx API keys stay server-side.
"""

import os

import httpx
from loguru import logger

from api.db import db_client
from api.services.telephony.dialer.provider import DialerCredentials


class TelnyxNotConfigured(Exception):
    """Raised when Telnyx WebRTC credentials cannot be issued."""


class TelnyxDialerProvider:
    name = "telnyx"

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        saved = None
        try:
            saved = await db_client.get_platform_telnyx_dialer_credentials()
        except Exception as exc:  # noqa: BLE001 - retain environment fallback on DB issues
            logger.warning(f"Could not load saved Telnyx dialer credentials: {type(exc).__name__}")
        api_key = ((saved or {}).get("api_key") or os.environ.get("TELNYX_API_KEY") or "").strip()
        credential_id = (
            (saved or {}).get("telephony_credential_id")
            or os.environ.get("TELNYX_DIALER_CREDENTIAL_ID")
            or ""
        ).strip()
        if not api_key or not credential_id:
            missing = [
                name
                for name, value in (
                    ("TELNYX_API_KEY", api_key),
                    ("TELNYX_DIALER_CREDENTIAL_ID", credential_id),
                )
                if not value
            ]
            raise TelnyxNotConfigured(
                "Telnyx dialer is not configured: missing " + ", ".join(missing)
            )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.telnyx.com/v2/telephony_credentials/{credential_id}/token",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "text/plain"},
                    timeout=10.0,
                )
                response.raise_for_status()
                token = response.text.strip().strip('"')
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "").strip()[:300]
            logger.error(
                f"Telnyx WebRTC token request failed (HTTP {exc.response.status_code}): {detail}"
            )
            raise TelnyxNotConfigured(
                f"Telnyx would not issue a WebRTC token (HTTP {exc.response.status_code})."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - a token failure is a hard setup failure
            logger.error(f"Telnyx WebRTC token request failed: {type(exc).__name__}")
            raise TelnyxNotConfigured("Could not issue a Telnyx WebRTC token.") from exc

        if not token or token.startswith("{"):
            raise TelnyxNotConfigured("Telnyx returned no usable WebRTC token.")

        return DialerCredentials(
            token=token,
            identity=f"rep-{user_id}",
            destination="",
        )
