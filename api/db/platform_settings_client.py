"""
Platform-level (non-org-scoped) settings storage.

Holds the platform Twilio accounts used by Quick Connect to provision/forward
numbers on behalf of any org, and by the internal sales-rep dialer. Multiple
accounts may be stored at once; exactly one is ``is_active`` at a time, and
that is the one resolved by ``get_platform_twilio_credentials`` /
``get_platform_dialer_credentials``. The auth token and API key secret are
encrypted at rest (see ``api.services.crypto``); SIDs are stored in clear so
the admin UI can render masked previews without decrypting.
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
        Return ``{"account_sid", "auth_token", "last_validated_at"}`` for the
        active account, with the token decrypted, or ``None`` if none is
        active. Decryption failures (e.g. OSS_JWT_SECRET rotated) are treated
        as "not configured".
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

    async def get_platform_twilio_credentials_by_id(self, account_id: int) -> Optional[dict]:
        """
        Return ``{"account_sid", "auth_token"}`` for a *specific* stored
        account (regardless of ``is_active``), or ``None`` if it doesn't exist
        or fails to decrypt. Lets an admin choose which platform Twilio
        account to buy a number under, rather than always "the active one".
        """
        async with self.async_session() as session:
            row = await session.get(PlatformTwilioCredentialsModel, account_id)
        if not row:
            return None
        try:
            token = decrypt_secret(row.auth_token_encrypted)
        except Exception:
            return None
        return {"account_sid": row.account_sid, "auth_token": token}

    async def get_platform_twilio_sid(self) -> Optional[str]:
        """Return the active account's SID (no decryption), or ``None``."""
        async with self.async_session() as session:
            result = await session.execute(
                select(PlatformTwilioCredentialsModel.account_sid)
                .where(PlatformTwilioCredentialsModel.is_active.is_(True))
                .order_by(PlatformTwilioCredentialsModel.id.desc())
                .limit(1)
            )
            return result.scalars().first()

    async def get_platform_dialer_credentials(self) -> Optional[dict]:
        """
        Return ``{"account_sid", "api_key_sid", "api_key_secret",
        "twiml_app_sid", "default_caller_id"}`` for the active account, or
        ``None`` if no account is active, the active account has no dialer
        fields configured, or decryption fails. Callers should fall back to
        env vars on ``None`` (see ``voice_sdk.py``).
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(PlatformTwilioCredentialsModel)
                .where(PlatformTwilioCredentialsModel.is_active.is_(True))
                .order_by(PlatformTwilioCredentialsModel.id.desc())
                .limit(1)
            )
            row = result.scalars().first()
        if not row or not row.dialer_api_key_sid or not row.dialer_api_key_secret_encrypted:
            return None
        try:
            api_key_secret = decrypt_secret(row.dialer_api_key_secret_encrypted)
        except Exception:
            return None
        return {
            "account_sid": row.account_sid,
            "api_key_sid": row.dialer_api_key_sid,
            "api_key_secret": api_key_secret,
            "twiml_app_sid": row.dialer_twiml_app_sid,
            "default_caller_id": row.dialer_default_caller_id,
        }

    async def get_platform_dialer_auth_token(self) -> Optional[str]:
        """
        Return the active account's decrypted auth token, for verifying
        inbound Twilio webhook signatures on the dialer's voice-connect route.
        """
        creds = await self.get_platform_twilio_credentials()
        return creds["auth_token"] if creds else None

    async def list_platform_twilio_accounts(self) -> list[dict]:
        """List every stored account (no secrets), most recent first."""
        async with self.async_session() as session:
            result = await session.execute(
                select(PlatformTwilioCredentialsModel).order_by(
                    PlatformTwilioCredentialsModel.id.desc()
                )
            )
            rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "label": r.label,
                "account_sid": r.account_sid,
                "is_active": r.is_active,
                "last_validated_at": r.last_validated_at,
                "created_at": r.created_at,
                "dialer_configured": bool(
                    r.dialer_api_key_sid and r.dialer_api_key_secret_encrypted
                ),
                # Identifiers, not secrets — safe to show in full so the edit
                # form can pre-fill them. Only auth_token / dialer_api_key_secret
                # are ever masked/withheld.
                "dialer_api_key_sid": r.dialer_api_key_sid,
                "dialer_twiml_app_sid": r.dialer_twiml_app_sid,
                "dialer_default_caller_id": r.dialer_default_caller_id,
            }
            for r in rows
        ]

    async def update_platform_twilio_account(
        self, account_id: int, updates: dict
    ) -> Optional[str]:
        """
        Partially update a stored account. *updates* keys present are applied;
        keys absent are left untouched (this is the caller's job to enforce —
        pass only fields the client actually submitted, e.g. via
        ``BaseModel.model_dump(exclude_unset=True)``).

        Recognised keys: label, account_sid, auth_token, dialer_api_key_sid,
        dialer_api_key_secret, dialer_twiml_app_sid, dialer_default_caller_id.
        Empty string clears a nullable field; ``auth_token``/
        ``dialer_api_key_secret`` are re-encrypted when present and non-empty,
        left alone when absent, and only cleared (along with the paired SID)
        by explicitly clearing ``dialer_api_key_sid`` to "".

        Returns None on success, or an error string if the account doesn't exist.
        """
        async with self.async_session() as session:
            row = await session.get(PlatformTwilioCredentialsModel, account_id)
            if not row:
                return "account not found"

            if "label" in updates:
                row.label = updates["label"] or None
            if "account_sid" in updates and updates["account_sid"]:
                row.account_sid = updates["account_sid"]
            if "auth_token" in updates and updates["auth_token"]:
                row.auth_token_encrypted = encrypt_secret(updates["auth_token"])
            if "dialer_api_key_sid" in updates:
                new_sid = updates["dialer_api_key_sid"] or None
                row.dialer_api_key_sid = new_sid
                if new_sid is None:
                    row.dialer_api_key_secret_encrypted = None
            if updates.get("dialer_api_key_secret"):
                row.dialer_api_key_secret_encrypted = encrypt_secret(
                    updates["dialer_api_key_secret"]
                )
            if "dialer_twiml_app_sid" in updates:
                row.dialer_twiml_app_sid = updates["dialer_twiml_app_sid"] or None
            if "dialer_default_caller_id" in updates:
                row.dialer_default_caller_id = (
                    updates["dialer_default_caller_id"] or None
                )

            await session.commit()
            return None

    async def add_platform_twilio_account(
        self,
        account_sid: str,
        auth_token: str,
        label: Optional[str] = None,
        make_active: bool = False,
        dialer_api_key_sid: Optional[str] = None,
        dialer_api_key_secret: Optional[str] = None,
        dialer_twiml_app_sid: Optional[str] = None,
        dialer_default_caller_id: Optional[str] = None,
    ) -> int:
        """
        Insert a new platform Twilio account. Existing accounts (and their
        ``is_active`` state) are left untouched unless ``make_active`` is set,
        in which case every other row is deactivated first so exactly one
        remains active. Returns the new row's id.
        """
        encrypted = encrypt_secret(auth_token)
        dialer_secret_encrypted = (
            encrypt_secret(dialer_api_key_secret) if dialer_api_key_secret else None
        )
        now = datetime.now(UTC)
        async with self.async_session() as session:
            if make_active:
                existing = await session.execute(
                    select(PlatformTwilioCredentialsModel).where(
                        PlatformTwilioCredentialsModel.is_active.is_(True)
                    )
                )
                for r in existing.scalars().all():
                    r.is_active = False
            row = PlatformTwilioCredentialsModel(
                label=label,
                account_sid=account_sid,
                auth_token_encrypted=encrypted,
                dialer_api_key_sid=dialer_api_key_sid,
                dialer_api_key_secret_encrypted=dialer_secret_encrypted,
                dialer_twiml_app_sid=dialer_twiml_app_sid,
                dialer_default_caller_id=dialer_default_caller_id,
                is_active=make_active,
                last_validated_at=now,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def set_active_platform_twilio_account(self, account_id: int) -> bool:
        """
        Make *account_id* the sole active account. Returns False if no row
        with that id exists.
        """
        async with self.async_session() as session:
            target = await session.get(PlatformTwilioCredentialsModel, account_id)
            if not target:
                return False
            existing = await session.execute(
                select(PlatformTwilioCredentialsModel).where(
                    PlatformTwilioCredentialsModel.is_active.is_(True)
                )
            )
            for r in existing.scalars().all():
                r.is_active = False
            target.is_active = True
            await session.commit()
            return True

    async def delete_platform_twilio_account(self, account_id: int) -> Optional[str]:
        """
        Delete a stored account. Returns None on success, or an error string
        if the account doesn't exist or is currently active (callers should
        activate a different account first, or explicitly deactivate).
        """
        async with self.async_session() as session:
            row = await session.get(PlatformTwilioCredentialsModel, account_id)
            if not row:
                return "account not found"
            if row.is_active:
                return "cannot delete the active account — activate another first"
            await session.delete(row)
            await session.commit()
            return None

    async def clear_platform_twilio_credentials(self) -> bool:
        """
        Deactivate the active platform Twilio row (revert to env-var
        fallback). Returns True if a row was cleared.
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
