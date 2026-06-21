"""Database client for voice library operations."""

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Optional

from loguru import logger
from sqlalchemy import and_, or_, select

from api.db.base_client import BaseDBClient
from api.db.models import VoiceLibraryModel


class VoiceLibraryClient(BaseDBClient):

    async def create_voice(
        self,
        user_id: int,
        organization_id: int,
        name: str,
        provider: str,
        description: Optional[str] = None,
        is_public: bool = False,
        language: Optional[str] = None,
        accent: Optional[str] = None,
        gender: Optional[str] = None,
        age: Optional[str] = None,
        use_case: Optional[str] = None,
        provider_voice_id: Optional[str] = None,
        audio_preview_url: Optional[str] = None,
        labels: Optional[dict] = None,
        status: str = "pending",
    ) -> VoiceLibraryModel:
        async with self.async_session() as session:
            voice = VoiceLibraryModel(
                uuid=str(uuid_lib.uuid4()),
                user_id=user_id,
                organization_id=organization_id,
                name=name,
                description=description,
                provider=provider,
                provider_voice_id=provider_voice_id,
                language=language,
                accent=accent,
                gender=gender,
                age=age,
                use_case=use_case,
                is_public=is_public,
                status=status,
                audio_preview_url=audio_preview_url,
                labels=labels or {},
            )
            session.add(voice)
            await session.commit()
            await session.refresh(voice)
            logger.info(f"Created voice library entry {voice.uuid} for user {user_id}")
            return voice

    async def get_voice_by_uuid(
        self, voice_uuid: str, organization_id: int
    ) -> Optional[VoiceLibraryModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(VoiceLibraryModel).where(
                    and_(
                        VoiceLibraryModel.uuid == voice_uuid,
                        VoiceLibraryModel.organization_id == organization_id,
                    )
                )
            )
            return result.scalars().first()

    async def get_voice_by_uuid_any_org(
        self, voice_uuid: str
    ) -> Optional[VoiceLibraryModel]:
        """Look up a voice by UUID across all orgs. Used as a platform-level fallback."""
        async with self.async_session() as session:
            result = await session.execute(
                select(VoiceLibraryModel).where(VoiceLibraryModel.uuid == voice_uuid)
            )
            return result.scalars().first()

    async def get_voice_by_provider_id(
        self, provider_voice_id: str, organization_id: int
    ) -> Optional[VoiceLibraryModel]:
        """Look up an existing voice by its provider-assigned ID within an org."""
        async with self.async_session() as session:
            result = await session.execute(
                select(VoiceLibraryModel).where(
                    and_(
                        VoiceLibraryModel.provider_voice_id == provider_voice_id,
                        VoiceLibraryModel.organization_id == organization_id,
                    )
                )
            )
            return result.scalars().first()

    async def list_voices(
        self,
        organization_id: int,
        user_id: int,
        is_superuser: bool = False,
        language: Optional[str] = None,
        gender: Optional[str] = None,
        age: Optional[str] = None,
        use_case: Optional[str] = None,
        provider: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[VoiceLibraryModel]:
        async with self.async_session() as session:
            # Non-ready voices (pending/processing/failed) are always private to their
            # creator — regardless of is_public or superuser status.
            # Superusers see all ready voices across ALL orgs (no org filter).
            if is_superuser:
                base_filter = or_(
                    VoiceLibraryModel.status == "ready",
                    VoiceLibraryModel.user_id == user_id,
                )
            else:
                base_filter = and_(
                    VoiceLibraryModel.organization_id == organization_id,
                    or_(
                        and_(VoiceLibraryModel.is_public == True, VoiceLibraryModel.status == "ready"),
                        VoiceLibraryModel.user_id == user_id,
                    ),
                )

            filters = [base_filter]
            if language:
                filters.append(VoiceLibraryModel.language == language)
            if gender:
                filters.append(VoiceLibraryModel.gender == gender)
            if age:
                filters.append(VoiceLibraryModel.age == age)
            if use_case:
                filters.append(VoiceLibraryModel.use_case == use_case)
            if provider:
                filters.append(VoiceLibraryModel.provider == provider)
            if status:
                filters.append(VoiceLibraryModel.status == status)

            result = await session.execute(
                select(VoiceLibraryModel)
                .where(and_(*filters))
                .order_by(VoiceLibraryModel.created_at.desc())
            )
            return list(result.scalars().all())

    async def update_voice(
        self,
        voice_uuid: str,
        organization_id: int,
        user_id: int,
        is_superuser: bool = False,
        **kwargs,
    ) -> Optional[VoiceLibraryModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(VoiceLibraryModel).where(
                    and_(
                        VoiceLibraryModel.uuid == voice_uuid,
                        VoiceLibraryModel.organization_id == organization_id,
                    )
                )
            )
            voice = result.scalars().first()
            if not voice:
                return None
            if not is_superuser and voice.user_id != user_id:
                return None
            allowed = {"name", "description", "is_public", "language", "accent", "gender", "age", "use_case"}
            for key, value in kwargs.items():
                if key in allowed and value is not None:
                    setattr(voice, key, value)
            voice.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(voice)
            return voice

    async def update_voice_status(
        self,
        voice_uuid: str,
        status: str,
        provider_voice_id: Optional[str] = None,
        audio_preview_url: Optional[str] = None,
        labels_patch: Optional[dict] = None,
    ) -> None:
        async with self.async_session() as session:
            result = await session.execute(
                select(VoiceLibraryModel).where(VoiceLibraryModel.uuid == voice_uuid)
            )
            voice = result.scalars().first()
            if not voice:
                logger.warning(f"update_voice_status: voice {voice_uuid} not found")
                return
            voice.status = status
            voice.updated_at = datetime.now(UTC)
            if provider_voice_id:
                voice.provider_voice_id = provider_voice_id
            if audio_preview_url:
                voice.audio_preview_url = audio_preview_url
            if labels_patch:
                from sqlalchemy import update as sa_update
                merged = dict(voice.labels or {})
                merged.update(labels_patch)
                await session.execute(
                    sa_update(VoiceLibraryModel)
                    .where(VoiceLibraryModel.uuid == voice_uuid)
                    .values(labels=merged)
                )
            await session.commit()

    async def delete_voice(
        self,
        voice_uuid: str,
        organization_id: int,
        user_id: int,
        is_superuser: bool = False,
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                select(VoiceLibraryModel).where(
                    and_(
                        VoiceLibraryModel.uuid == voice_uuid,
                        VoiceLibraryModel.organization_id == organization_id,
                    )
                )
            )
            voice = result.scalars().first()
            if not voice:
                return False
            if not is_superuser and voice.user_id != user_id:
                return False
            await session.delete(voice)
            await session.commit()
            return True
