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

    @staticmethod
    async def _request_token(
        client: httpx.AsyncClient, *, api_key: str, credential_id: str
    ) -> str:
        response = await client.post(
            f"https://api.telnyx.com/v2/telephony_credentials/{credential_id}/token",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "text/plain"},
            timeout=10.0,
        )
        response.raise_for_status()
        return response.text.strip().strip('"')

    async def _get_or_create_user_credential(
        self, *, client: httpx.AsyncClient, api_key: str, connection_id: str, user_id: int
    ) -> str:
        saved = await db_client.get_platform_telnyx_user_credential(user_id)
        credential_id = (saved or {}).get("telephony_credential_id", "")
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

        if saved and saved.get("connection_id") == connection_id and credential_id:
            check = await client.get(
                f"https://api.telnyx.com/v2/telephony_credentials/{credential_id}",
                headers=headers,
                timeout=10.0,
            )
            if check.is_success:
                data = check.json().get("data") or {}
                if (
                    data.get("resource_id") == f"connection:{connection_id}"
                    and not data.get("expired", False)
                ):
                    return credential_id
            elif check.status_code not in (404, 422):
                check.raise_for_status()

        created = await client.post(
            "https://api.telnyx.com/v2/telephony_credentials",
            headers=headers,
            json={"connection_id": connection_id, "name": f"Sysevo Dialer Rep {user_id}"},
            timeout=15.0,
        )
        created.raise_for_status()
        credential_id = (created.json().get("data") or {}).get("id", "")
        if not credential_id:
            raise TelnyxNotConfigured("Telnyx created no usable WebRTC credential for this rep.")
        await db_client.save_platform_telnyx_user_credential(
            user_id=user_id,
            connection_id=connection_id,
            telephony_credential_id=credential_id,
        )
        return credential_id

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        saved = None
        try:
            saved = await db_client.get_platform_telnyx_dialer_credentials()
        except Exception as exc:  # noqa: BLE001 - never use stale fallback settings on DB failure
            logger.error(f"Could not load saved Telnyx dialer settings: {type(exc).__name__}")
            raise TelnyxNotConfigured(
                "Could not verify the saved Telnyx dialer settings. Please retry; no call was placed."
            ) from exc
        api_key = ((saved or {}).get("api_key") or os.environ.get("TELNYX_API_KEY") or "").strip()
        connection_id = ((saved or {}).get("connection_id") or "").strip()
        # When the admin settings are stored in the database, create an
        # individual Telnyx credential for each rep. Keep the env-only setup
        # as a backwards-compatible single credential fallback.
        credential_id = ""
        if not saved:
            credential_id = (os.environ.get("TELNYX_DIALER_CREDENTIAL_ID") or "").strip()
        if not api_key or (not saved and not credential_id) or (saved and not connection_id):
            missing = [
                name
                for name, value in (
                    ("TELNYX_API_KEY", api_key),
                    ("Telnyx SIP connection ID", connection_id or credential_id),
                )
                if not value
            ]
            if missing:
                raise TelnyxNotConfigured("Telnyx dialer is not configured: missing " + ", ".join(missing))

        try:
            async with httpx.AsyncClient() as client:
                if saved:
                    credential_id = await self._get_or_create_user_credential(
                        client=client,
                        api_key=api_key,
                        connection_id=connection_id,
                        user_id=user_id,
                    )
                token = await self._request_token(
                    client, api_key=api_key, credential_id=credential_id
                )
        except TelnyxNotConfigured:
            raise
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "").strip()[:300]
            logger.error(
                f"Telnyx WebRTC credential/token request failed (HTTP {exc.response.status_code}): {detail}"
            )
            raise TelnyxNotConfigured(
                f"Telnyx could not prepare WebRTC credentials (HTTP {exc.response.status_code})."
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
