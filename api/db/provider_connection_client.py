"""CRUD client for org-level provider connections and available models."""

from typing import List, Optional

import httpx
from loguru import logger
from sqlalchemy import delete, select

from api.db.base_client import BaseDBClient
from api.db.models import OrgAvailableModelModel, OrgProviderConnectionModel
from api.services.configuration.pricing import get_model_pricing


# Comprehensive fallback catalog — used when live fetch fails or provider has no listing API.
PROVIDER_MODELS: dict[str, dict[str, list[str]]] = {
    "llm": {
        "openai": [
            "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
            "gpt-4o", "gpt-4o-mini",
            "o3", "o4-mini", "o1",
            "gpt-4-turbo", "gpt-3.5-turbo",
        ],
        "anthropic": [
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ],
        "google": [
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
            "gemini-2.0-flash", "gemini-2.0-flash-lite",
            "gemini-1.5-pro", "gemini-1.5-flash",
        ],
        "groq": [
            "llama-3.3-70b-versatile",
            "deepseek-r1-distill-llama-70b",
            "qwen-qwq-32b",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "gemma2-9b-it",
            "llama-3.1-8b-instant",
        ],
        "xai": [
            "grok-3", "grok-3-mini", "grok-3-fast", "grok-3-mini-fast",
            "grok-2-1212", "grok-2-vision-1212", "grok-beta",
        ],
        "openrouter": [
            "openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4o", "openai/o3",
            "anthropic/claude-opus-4", "anthropic/claude-sonnet-4", "anthropic/claude-haiku-4-5",
            "google/gemini-2.5-pro", "google/gemini-2.5-flash", "google/gemini-2.0-flash",
            "meta-llama/llama-3.3-70b-instruct", "deepseek/deepseek-chat-v3-0324",
        ],
        "azure": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-35-turbo"],
        "aws_bedrock": [
            "us.amazon.nova-pro-v1:0", "us.amazon.nova-lite-v1:0", "us.amazon.nova-micro-v1:0",
            "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
        "google_vertex": [
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
            "gemini-2.0-flash", "gemini-1.5-pro-002", "gemini-1.5-flash-002",
        ],
        "minimax": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-Text-01"],
        "speaches": ["custom"],
        "mistral": [
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "mistral-nemo",
            "codestral-latest",
            "open-mistral-7b",
            "open-mixtral-8x7b",
            "open-mixtral-8x22b",
        ],
        "together": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Llama-3.1-405B-Instruct-Turbo",
            "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-V3",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
            "mistralai/Mistral-7B-Instruct-v0.3",
            "google/gemma-2-27b-it",
        ],
        "cerebras": [
            "llama3.3-70b",
            "llama3.1-70b",
            "llama3.1-8b",
            "llama3.1-405b",
        ],
        "fireworks": [
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/llama-v3p1-8b-instruct",
            "accounts/fireworks/models/llama-v3p1-405b-instruct",
            "accounts/fireworks/models/mixtral-8x7b-instruct",
            "accounts/fireworks/models/qwen2p5-72b-instruct",
            "accounts/fireworks/models/deepseek-v3",
        ],
        "cohere": [
            "command-r-plus-08-2024",
            "command-r-08-2024",
            "command-r",
            "command-light",
            "command-r7b-12-2024",
        ],
    },
    "tts": {
        "elevenlabs": [
            "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2",
            "eleven_turbo_v2", "eleven_monolingual_v1", "eleven_multilingual_v1",
            "eleven_multilingual_sts_v2", "eleven_english_sts_v2",
        ],
        "openai": ["gpt-4o-mini-tts", "gpt-4o-audio-preview", "tts-1", "tts-1-hd"],
        "xai": ["grok-voice-latest"],
        "deepgram": [
            "aura-2-asteria-en", "aura-asteria-en", "aura-luna-en",
            "aura-stella-en", "aura-orion-en", "aura-zeus-en", "aura-arcas-en",
        ],
        "cartesia": ["sonic-3", "sonic-2", "sonic-english", "sonic-multilingual"],
        "google": ["chirp_3_hd", "en-US-Neural2-F", "en-US-Neural2-A", "en-US-Wavenet-A"],
        "rime": ["arcana", "mistv3", "mistv2", "mist", "arcas", "cove", "marsh"],
        "camb": ["mars-flash", "mars-pro", "mars-instruct"],
        "sarvam": ["bulbul:v2", "bulbul:v3"],
        "minimax": ["speech-02-hd", "speech-02-turbo", "speech-01-turbo", "speech-01"],
        "speaches": ["tts-1", "tts-1-hd", "hexgrad/Kokoro-82M"],
        "aws_polly": ["neural", "long-form", "standard", "generative"],
        "azure_tts": ["neural", "standard"],
        "playht": ["Play3.0-mini", "PlayHT2.0-turbo", "PlayHT2.0", "PlayDialog"],
        "neets": ["style-diff-500", "ar-diff-50k", "vits"],
    },
    "stt": {
        "deepgram": [
            "nova-3-general", "nova-3", "nova-2-general", "nova-2",
            "nova-2-meeting", "nova-2-phonecall", "nova-2-finance",
            "flux-general-en", "flux-general-multi",
        ],
        "openai": ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
        "xai": ["xai-speech-v1"],
        "assemblyai": ["u3-rt-pro", "u3-rt-standard", "best", "nano"],
        "google": ["chirp_2", "chirp", "latest_long", "latest_short"],
        "groq": ["whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"],
        "gladia": ["solaria-1", "accurate", "fast"],
        "speechmatics": ["enhanced", "standard"],
        "sarvam": ["saarika:v2"],
        "speaches": ["whisper-1"],
        "azure_speech": ["realtime", "whisper"],
        "aws_transcribe": ["general", "phone-call", "dictation"],
    },
    "embeddings": {
        "openai": [
            "text-embedding-3-large", "text-embedding-3-small", "text-embedding-ada-002",
        ],
        "openrouter": ["openai/text-embedding-3-large", "openai/text-embedding-3-small"],
    },
    "realtime": {
        "openai_realtime": ["gpt-4o-realtime-preview", "gpt-4o-mini-realtime-preview"],
        "google_realtime": ["gemini-2.0-flash-live-001", "gemini-2.0-flash-exp"],
        "google_vertex_realtime": ["gemini-2.0-flash-exp"],
        "grok_realtime": ["grok-beta"],
        "ultravox_realtime": ["fixie-ai/ultravox-70B"],
    },
}


async def _fetch_live_models(provider: str, service_type: str, api_key: Optional[str]) -> list[str]:
    """Try to fetch current model list from provider's live API. Returns [] on any failure."""
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # ── OpenAI ─────────────────────────────────────────────────────────
            if provider == "openai":
                r = await client.get("https://api.openai.com/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict)]
                # Things that are never chat/completion models
                _exclude = ("-realtime-", "search-preview", "chatgpt-image", "diarize",
                            "dall-e", "babbage", "davinci", "ada", "curie", "moderation",
                            "whisper", "tts", "transcribe", "embedding")
                if service_type == "llm":
                    return sorted(
                        m for m in ids
                        if not any(x in m for x in _exclude)
                        and any(m.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "chatgpt-4o"))
                    )
                if service_type == "tts":
                    return sorted(m for m in ids
                                  if m.startswith("tts-") or "-tts" in m or "audio-preview" in m)
                if service_type == "stt":
                    return sorted(m for m in ids
                                  if "whisper" in m or "transcribe" in m)
                if service_type == "embeddings":
                    return sorted(m for m in ids if "embedding" in m)

            # ── xAI ────────────────────────────────────────────────────────────
            elif provider == "xai":
                r = await client.get("https://api.x.ai/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict)]
                if service_type == "llm":
                    # Exclude image/video generation, keep chat/reasoning models
                    return sorted(m for m in ids
                                  if m.startswith("grok-")
                                  and not any(x in m for x in ("imagine", "image", "video")))
                if service_type == "tts":
                    return sorted(m for m in ids if "voice" in m or "audio" in m or "tts" in m)

            # ── Groq ───────────────────────────────────────────────────────────
            elif provider == "groq":
                r = await client.get("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict)]
                if service_type == "stt":
                    return sorted(m for m in ids if "whisper" in m.lower() or "distil" in m.lower())
                if service_type == "llm":
                    stt_ids = {m for m in ids if "whisper" in m.lower() or "distil" in m.lower()}
                    return [m for m in ids if m not in stt_ids]

            # ── Anthropic ──────────────────────────────────────────────────────
            elif provider == "anthropic" and service_type == "llm":
                r = await client.get("https://api.anthropic.com/v1/models",
                                     headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
                if r.status_code != 200:
                    return []
                return [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]

            # ── Google Gemini ──────────────────────────────────────────────────
            elif provider == "google" and service_type == "llm":
                r = await client.get("https://generativelanguage.googleapis.com/v1beta/models",
                                     params={"key": api_key})
                if r.status_code != 200:
                    return []
                ids = []
                for m in r.json().get("models", []):
                    if not isinstance(m, dict):
                        continue
                    if "generateContent" not in m.get("supportedGenerationMethods", []):
                        continue
                    model_id = m.get("name", "").removeprefix("models/")
                    if model_id:
                        ids.append(model_id)
                return sorted(ids)

            # ── ElevenLabs ─────────────────────────────────────────────────────
            elif provider == "elevenlabs" and service_type == "tts":
                r = await client.get("https://api.elevenlabs.io/v1/models",
                                     headers={"xi-api-key": api_key})
                if r.status_code != 200:
                    return []
                return [m["model_id"] for m in r.json() if isinstance(m, dict) and m.get("model_id")]

            # ── Deepgram ───────────────────────────────────────────────────────
            elif provider == "deepgram":
                r = await client.get("https://api.deepgram.com/v1/models",
                                     headers={"Authorization": f"Token {api_key}"})
                if r.status_code != 200:
                    return []
                data = r.json()
                if service_type == "stt":
                    stt_models = data.get("stt", [])
                    return sorted(
                        m.get("canonical_name") or m.get("name", "")
                        for m in stt_models if isinstance(m, dict)
                        if m.get("canonical_name") or m.get("name")
                    )
                if service_type == "tts":
                    tts_models = data.get("tts", [])
                    return sorted(
                        m.get("canonical_name") or m.get("name", "")
                        for m in tts_models if isinstance(m, dict)
                        if m.get("canonical_name") or m.get("name")
                    )

            # ── Cartesia ───────────────────────────────────────────────────────
            elif provider == "cartesia" and service_type == "tts":
                r = await client.get("https://api.cartesia.ai/models",
                                     headers={"X-API-Key": api_key, "Cartesia-Version": "2025-04-16"})
                if r.status_code != 200:
                    return []
                return sorted(
                    m.get("id", "") for m in r.json()
                    if isinstance(m, dict) and m.get("id")
                )

            # ── OpenRouter ─────────────────────────────────────────────────────
            elif provider == "openrouter" and service_type == "llm":
                r = await client.get("https://openrouter.ai/api/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                return sorted(
                    m.get("id", "") for m in r.json().get("data", [])
                    if isinstance(m, dict) and m.get("id")
                )

            # ── Mistral AI ─────────────────────────────────────────────────────
            elif provider == "mistral" and service_type == "llm":
                r = await client.get("https://api.mistral.ai/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                return sorted(
                    m.get("id", "") for m in r.json().get("data", [])
                    if isinstance(m, dict) and m.get("id")
                )

            # ── Together AI ────────────────────────────────────────────────────
            elif provider == "together" and service_type == "llm":
                r = await client.get("https://api.together.xyz/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                data = r.json()
                items = data if isinstance(data, list) else data.get("data", [])
                return sorted(
                    m.get("id", "") for m in items
                    if isinstance(m, dict) and m.get("id")
                    and m.get("type", "") in ("chat", "language", "")
                )

            # ── Cerebras ───────────────────────────────────────────────────────
            elif provider == "cerebras" and service_type == "llm":
                r = await client.get("https://api.cerebras.ai/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                return sorted(
                    m.get("id", "") for m in r.json().get("data", [])
                    if isinstance(m, dict) and m.get("id")
                )

            # ── Fireworks AI ───────────────────────────────────────────────────
            elif provider == "fireworks" and service_type == "llm":
                r = await client.get("https://api.fireworks.ai/inference/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                return sorted(
                    m.get("id", "") for m in r.json().get("data", [])
                    if isinstance(m, dict) and m.get("id")
                )

            # ── Cohere ─────────────────────────────────────────────────────────
            elif provider == "cohere" and service_type == "llm":
                r = await client.get("https://api.cohere.com/v2/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code != 200:
                    return []
                return sorted(
                    m.get("name", "") for m in r.json().get("models", [])
                    if isinstance(m, dict) and m.get("name")
                    and "command" in m.get("name", "").lower()
                )

    except Exception as exc:
        logger.warning(f"Live model fetch failed for {provider}/{service_type}: {exc}")
    return []


class ProviderConnectionClient(BaseDBClient):

    async def get_default_embedding_model_connection(
        self, organization_id: int
    ) -> Optional[tuple]:
        """Return (api_key, model_id, base_url) for the org's default embedding model, or None."""
        async with self.async_session() as session:
            model_result = await session.execute(
                select(OrgAvailableModelModel).where(
                    OrgAvailableModelModel.organization_id == organization_id,
                    OrgAvailableModelModel.service_type == "embeddings",
                    OrgAvailableModelModel.is_default == True,
                )
            )
            model = model_result.scalar_one_or_none()
            if not model:
                return None
            conn_result = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.id == model.connection_id,
                    OrgProviderConnectionModel.is_active == True,
                )
            )
            conn = conn_result.scalar_one_or_none()
            if conn and conn.api_key:
                return (
                    conn.api_key,
                    model.model_id,
                    (conn.extra_config or {}).get("base_url"),
                )
            return None

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
            # exists (even inactive), update it rather than failing.
            existing = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.organization_id == organization_id,
                    OrgProviderConnectionModel.service_type == service_type,
                    OrgProviderConnectionModel.provider == provider,
                )
            )
            conn = existing.scalars().first()

            if conn is not None:
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
                logger.info(f"Created provider connection {provider}/{service_type} for org {organization_id}")

            await session.commit()
            await session.refresh(conn)

        # Reseed models — failure here is non-fatal; the connection is already saved.
        # Users can manually sync models via the "Sync Models" button.
        try:
            await self.reseed_models_for_connection(conn.id, organization_id)
        except Exception as exc:
            logger.error(f"Model seeding failed for connection {conn.id} ({provider}/{service_type}): {exc}")
        return conn

    async def reseed_models_for_connection(
        self,
        connection_id: int,
        organization_id: int,
    ) -> int:
        """Re-seed org_available_models for a connection using live API + fallback catalog.
        Preserves is_client_available and is_default for models that already exist.
        Returns the number of models after reseed."""
        async with self.async_session() as session:
            conn_result = await session.execute(
                select(OrgProviderConnectionModel).where(
                    OrgProviderConnectionModel.id == connection_id,
                    OrgProviderConnectionModel.organization_id == organization_id,
                    OrgProviderConnectionModel.is_active == True,
                )
            )
            conn = conn_result.scalar_one_or_none()
            if not conn:
                return 0

            # Preserve existing visibility/default settings
            existing_result = await session.execute(
                select(OrgAvailableModelModel).where(
                    OrgAvailableModelModel.connection_id == connection_id,
                )
            )
            existing = {m.model_id: m for m in existing_result.scalars().all()}

        # Fetch live, fall back to catalog
        model_ids = await _fetch_live_models(conn.provider, conn.service_type, conn.api_key)
        if not model_ids:
            model_ids = PROVIDER_MODELS.get(conn.service_type, {}).get(conn.provider, [])

        if not model_ids:
            return 0

        # Deduplicate while preserving order (live API can return duplicates)
        model_ids = list(dict.fromkeys(model_ids))

        async with self.async_session() as session:
            # Delete all current models for this connection
            await session.execute(
                delete(OrgAvailableModelModel).where(
                    OrgAvailableModelModel.connection_id == connection_id,
                )
            )
            # Re-insert with preserved settings and auto-populated pricing
            for i, model_id in enumerate(model_ids):
                prev = existing.get(model_id)
                pricing = get_model_pricing(model_id)
                session.add(OrgAvailableModelModel(
                    connection_id=connection_id,
                    organization_id=organization_id,
                    service_type=conn.service_type,
                    model_id=model_id,
                    is_client_available=prev.is_client_available if prev else True,
                    is_default=prev.is_default if prev else (i == 0),
                    cost_per_min_usd=pricing[0] if pricing else (prev.cost_per_min_usd if prev else None),
                    native_cost_display=pricing[1] if pricing else (prev.native_cost_display if prev else None),
                ))
            await session.commit()

        logger.info(f"Reseeded {len(model_ids)} models for connection {connection_id} ({conn.provider}/{conn.service_type})")
        return len(model_ids)

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

    async def list_all_connections_superuser(
        self,
        service_type: Optional[str] = None,
    ) -> List[OrgProviderConnectionModel]:
        """Return all active connections across every org. Superuser only."""
        async with self.async_session() as session:
            query = select(OrgProviderConnectionModel).where(
                OrgProviderConnectionModel.is_active == True,
            )
            if service_type:
                query = query.where(OrgProviderConnectionModel.service_type == service_type)
            query = query.order_by(
                OrgProviderConnectionModel.service_type,
                OrgProviderConnectionModel.provider,
            )
            result = await session.execute(query)
            return list(result.scalars().all())

    async def list_all_available_models_superuser(
        self,
        service_type: Optional[str] = None,
    ) -> List[OrgAvailableModelModel]:
        """Return all models across every org. Superuser only."""
        async with self.async_session() as session:
            query = select(OrgAvailableModelModel)
            if service_type:
                query = query.where(OrgAvailableModelModel.service_type == service_type)
            query = query.order_by(
                OrgAvailableModelModel.service_type,
                OrgAvailableModelModel.model_id,
            )
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

    async def set_model_our_price(
        self,
        model_id_pk: int,
        organization_id: int,
        our_price_per_min_usd: Optional[float],
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
            m.our_price_per_min_usd = our_price_per_min_usd
            await session.commit()
            await session.refresh(m)
            return m

    async def set_model_display_name(
        self,
        model_id_pk: int,
        organization_id: int,
        display_name: Optional[str],
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
            m.display_name = display_name
            await session.commit()
            await session.refresh(m)
            return m

    async def set_model_cost(
        self,
        model_id_pk: int,
        organization_id: int,
        cost_per_min_usd: float,
        native_cost_display: str,
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
            m.cost_per_min_usd = cost_per_min_usd
            m.native_cost_display = native_cost_display
            await session.commit()
            await session.refresh(m)
            return m
