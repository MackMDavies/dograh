"""
Platform-level (non-org-scoped) settings storage.

Currently holds the single platform Twilio account used by Quick Connect to
provision/forward numbers on behalf of any org. The auth token is encrypted at
rest (see ``api.services.crypto``); the account SID is stored in clear so the
admin UI can render a masked preview without decrypting.
"""
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import PlatformTwilioCredentialsModel
from api.services.crypto import decrypt_secret, encrypt_secret


class PlatformSettingsClient(BaseDBClient):
    async def get_platform_twilio_credentials(self) -> Optional[dict]:
        """
        Return ``{"account_sid", "auth_token", "last_validated_at"}`` with the
        token decrypted, or ``None`` if no active row exists. Decryption failures
        (e.g. OSS_JWT_SECRET rotated) are treated as "not configured".
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(PlatformTwilioCredentialsModel)
                .where(PlatformTwilioCredentialsModel.is_active.is_(True))
                .order_by(PlatformTwilioCredentialsModel.id.desc())
                .limit(1)
            )
            row = result.scalars().first()
        if not row:
            return None
        try:
            token = decrypt_secret(row.auth_token_encrypted)
        except Exception:
            return None
        return {
            "account_sid": row.account_sid,
            "auth_token": token,
            "last_validated_at": row.last_validated_at,
        }

    async def get_platform_twilio_sid(self) -> Optional[str]:
        """Return the stored account SID (no decryption), or ``None``."""
        async with self.async_session() as session:
            result = await session.execute(
                select(PlatformTwilioCredentialsModel.account_sid)
                .where(PlatformTwilioCredentialsModel.is_active.is_(True))
                .order_by(PlatformTwilioCredentialsModel.id.desc())
                .limit(1)
            )
            return result.scalars().first()

    async def save_platform_twilio_credentials(
        self, account_sid: str, auth_token: str
    ) -> None:
        """
        Upsert the single platform Twilio credentials row, encrypting the token.
        Deactivates any prior rows so exactly one active row remains.
        """
        encrypted = encrypt_secret(auth_token)
        now = datetime.now(UTC)
        async with self.async_session() as session:
            existing = await session.execute(
                select(PlatformTwilioCredentialsModel)
            )
            rows = existing.scalars().all()
            for r in rows:
                r.is_active = False
            session.add(
                PlatformTwilioCredentialsModel(
                    account_sid=account_sid,
                    auth_token_encrypted=encrypted,
                    is_active=True,
                    last_validated_at=now,
                )
            )
            await session.commit()

    async def clear_platform_twilio_credentials(self) -> bool:
        """
        Deactivate all platform Twilio rows (revert to env-var fallback).
        Returns True if any active row was cleared.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(PlatformTwilioCredentialsModel).where(
                    PlatformTwilioCredentialsModel.is_active.is_(True)
                )
            )
            rows = result.scalars().all()
            if not rows:
                return False
            for r in rows:
                r.is_active = False
            await session.commit()
            return True
