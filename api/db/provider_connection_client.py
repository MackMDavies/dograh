"""CRUD client for org-level provider connections and available models."""

from typing import List, Optional

from loguru import logger
from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import OrgAvailableModelModel, OrgProviderConnectionModel


# Models that each provider exposes, sourced from registry.py model lists.
# Used to auto-seed OrgAvailableModelModel rows when a connection is created.
PROVIDER_MODELS: dict[str, dict[str, list[str]]] = {
    "llm": {
        "openai": [
            "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
            "gpt-5", "gpt-5-mini", "gpt-5-nano",
            "gpt-3.5-turbo",
        ],
        "google": [
            "gemini-2.0-flash", "gemini-2.0-flash-lite",
            "gemini-2.5-flash", "gemini-2.5-flash-lite",
            "gemini-3.5-flash", "gemini-3.5-flash-lite",
        ],
        "groq": [
            "llama-3.3-70b-versatile",
            "deepseek-r1-distill-llama-70b",
            "qwen-qwq-32b",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "gemma2-9b-it",
            "llama-3.1-8b-instant",
            "openai/gpt-oss-120b",
        ],
        "xai": [
            "grok-3",
            "grok-3-mini",
            "grok-3-fast",
            "grok-3-mini-fast",
            "grok-2-1212",
            "grok-2-vision-1212",
        ],
        "openrouter": [
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
            "anthropic/claude-sonnet-4",
            "google/gemini-2.5-flash",
            "google/gemini-2.0-flash",
            "meta-llama/llama-3.3-70b-instruct",
            "deepseek/deepseek-chat-v3-0324",
        ],
        "azure": ["gpt-4.1-mini"],
        "aws_bedrock": [
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-micro-v1:0",
            "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
        "google_vertex": [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
        ],
        "minimax": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed"],
        "speaches": ["custom"],
    },
    "tts": {
        "xai": ["grok-voice-latest"],
        "elevenlabs": ["eleven_turbo_v2_5", "eleven_multilingual_v2", "eleven_flash_v2_5"],
        "openai": ["tts-1", "tts-1-hd"],
        "deepgram": ["aura-2", "aura-asteria-en", "aura-luna-en", "aura-zeus-en", "aura-arcas-en"],
        "cartesia": ["sonic-2", "sonic-multilingual"],
        "google": ["chirp_3_hd", "en-US-Neural2-F", "en-US-Neural2-A"],
        "rime": ["arcas", "cove", "marsh"],
        "camb": ["default"],
        "sarvam": ["bulbul:v2"],
        "minimax": ["speech-02-hd", "speech-02-turbo"],
        "speaches": ["custom"],
    },
    "stt": {
        "xai": ["xai-speech-v1"],
        "deepgram": ["nova-3", "nova-2", "nova-2-general", "flux-general-en"],
        "openai": ["whisper-1", "gpt-4o-transcribe"],
        "google": ["latest_long", "latest_short"],
        "assemblyai": ["best", "nano"],
        "gladia": ["solaria-1", "fast"],
        "speechmatics": ["default", "enhanced"],
        "sarvam": ["saarika:v2"],
        "speaches": ["custom"],
    },
    "embeddings": {
        "openai": ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
        "openrouter": ["text-embedding-3-small"],
    },
    "realtime": {
        "openai_realtime": ["gpt-4o-realtime-preview", "gpt-4o-mini-realtime-preview"],
        "google_realtime": ["gemini-3.1-flash-live-preview"],
        "google_vertex_realtime": ["gemini-3.1-flash-live-preview"],
        "grok_realtime": ["grok-3"],
        "ultravox_realtime": ["fixie-ai/ultravox-70B"],
    },
}


class ProviderConnectionClient(BaseDBClient):

    async def list_connections(
        self,
        organization_id: int,
        service_type: Optional[str] = None,
    ) -> List[OrgProviderConnectionModel]:
        async with self.async_session() as session:
            query = select(OrgProviderConnectionModel).where(
                OrgProviderConnectionModel.organization_id == organization_id,
                OrgProviderConnectionModel.is_active == True,
            )
            if service_type:
                query = query.where(OrgProviderConnectionModel.service_type == service_type)
            query = query.order_by(OrgProviderConnectionModel.service_type, OrgProviderConnectionModel.provider)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_connection(
        self, connection_id: int, organization_id: int
    ) -> Optional[OrgProviderConnectionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.id == connection_id,
                    OrgProviderConnectionModel.organization_id == organization_id,
                    OrgProviderConnectionModel.is_active == True,
                )
            )
            return result.scalar_one_or_none()

    async def create_connection(
        self,
        organization_id: int,
        created_by: int,
        service_type: str,
        provider: str,
        api_key: Optional[str],
        extra_config: dict,
        display_name: Optional[str] = None,
    ) -> OrgProviderConnectionModel:
        async with self.async_session() as session:
            # Upsert: if a connection for this (org, service_type, provider) already
            # exists (even inactive), update it rather than failing with a unique
            # constraint violation.
            existing = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.organization_id == organization_id,
                    OrgProviderConnectionModel.service_type == service_type,
                    OrgProviderConnectionModel.provider == provider,
                )
            )
            conn = existing.scalar_one_or_none()

            if conn is not None:
                # Re-activate and update credentials
                if api_key is not None:
                    conn.api_key = api_key
                if extra_config:
                    conn.extra_config = extra_config
                if display_name is not None:
                    conn.display_name = display_name
                conn.is_active = True
                logger.info(f"Updated existing provider connection {provider}/{service_type} for org {organization_id}")
            else:
                conn = OrgProviderConnectionModel(
                    organization_id=organization_id,
                    created_by=created_by,
                    service_type=service_type,
                    provider=provider,
                    api_key=api_key,
                    extra_config=extra_config,
                    display_name=display_name,
                )
                session.add(conn)
                await session.flush()  # get conn.id before inserting models

                # Seed available models from PROVIDER_MODELS (new connections only)
                model_ids = PROVIDER_MODELS.get(service_type, {}).get(provider, [])
                for i, model_id in enumerate(model_ids):
                    model = OrgAvailableModelModel(
                        connection_id=conn.id,
                        organization_id=organization_id,
                        service_type=service_type,
                        model_id=model_id,
                        is_client_available=True,
                        is_default=(i == 0),
                    )
                    session.add(model)

                logger.info(f"Created provider connection {provider}/{service_type} for org {organization_id}")

            await session.commit()
            await session.refresh(conn)
            return conn

    async def update_connection(
        self,
        connection_id: int,
        organization_id: int,
        api_key: Optional[str] = None,
        extra_config: Optional[dict] = None,
        display_name: Optional[str] = None,
    ) -> Optional[OrgProviderConnectionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.id == connection_id,
                    OrgProviderConnectionModel.organization_id == organization_id,
                )
            )
            conn = result.scalar_one_or_none()
            if not conn:
                return None
            if api_key is not None:
                conn.api_key = api_key
            if extra_config is not None:
                conn.extra_config = extra_config
            if display_name is not None:
                conn.display_name = display_name
            await session.commit()
            await session.refresh(conn)
            return conn

    async def delete_connection(self, connection_id: int, organization_id: int) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.id == connection_id,
                    OrgProviderConnectionModel.organization_id == organization_id,
                )
            )
            conn = result.scalar_one_or_none()
            if not conn:
                return False
            conn.is_active = False
            await session.commit()
            logger.info(f"Deleted connection {connection_id} for org {organization_id}")
            return True

    async def get_connection_by_provider(
        self,
        organization_id: int,
        service_type: str,
        provider: str,
    ) -> Optional[OrgProviderConnectionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.organization_id == organization_id,
                    OrgProviderConnectionModel.service_type == service_type,
                    OrgProviderConnectionModel.provider == provider,
                    OrgProviderConnectionModel.is_active == True,
                )
            )
            return result.scalars().first()

    async def list_available_models(
        self,
        organization_id: int,
        service_type: Optional[str] = None,
        client_only: bool = False,
    ) -> List[OrgAvailableModelModel]:
        async with self.async_session() as session:
            query = select(OrgAvailableModelModel).where(
                OrgAvailableModelModel.organization_id == organization_id,
            )
            if service_type:
                query = query.where(OrgAvailableModelModel.service_type == service_type)
            if client_only:
                query = query.where(OrgAvailableModelModel.is_client_available == True)
            query = query.order_by(OrgAvailableModelModel.service_type, OrgAvailableModelModel.model_id)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def set_model_client_available(
        self,
        model_id_pk: int,
        organization_id: int,
        is_client_available: bool,
    ) -> Optional[OrgAvailableModelModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrgAvailableModelModel).where(
                    OrgAvailableModelModel.id == model_id_pk,
                    OrgAvailableModelModel.organization_id == organization_id,
                )
            )
            m = result.scalar_one_or_none()
            if not m:
                return None
            m.is_client_available = is_client_available
            await session.commit()
            await session.refresh(m)
            return m

    async def set_model_default(
        self,
        model_id_pk: int,
        organization_id: int,
        service_type: str,
    ) -> Optional[OrgAvailableModelModel]:
        """Clear previous default for this service_type, set new one."""
        async with self.async_session() as session:
            # Clear existing default
            existing = await session.execute(
                select(OrgAvailableModelModel).where(
                    OrgAvailableModelModel.organization_id == organization_id,
                    OrgAvailableModelModel.service_type == service_type,
                    OrgAvailableModelModel.is_default == True,
                )
            )
            for m in existing.scalars().all():
                m.is_default = False

            # Set new default
            result = await session.execute(
                select(OrgAvailableModelModel).where(
                    OrgAvailableModelModel.id == model_id_pk,
                    OrgAvailableModelModel.organization_id == organization_id,
                )
            )
            m = result.scalar_one_or_none()
            if not m:
                return None
            m.is_default = True
            await session.commit()
            await session.refresh(m)
            return m
