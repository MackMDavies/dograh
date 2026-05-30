"""Resolve org-level provider connections into pipeline config objects.

Three public functions:
  resolve_org_provider_config  – fills None sections in UserConfiguration from org defaults
  enrich_overrides_with_org_api_keys – stamps missing API keys into model_overrides from org connections
  resolve_voice_for_tts        – resolves a voice library UUID → provider_voice_id string
"""

from __future__ import annotations

import copy

from loguru import logger

from api.db import db_client
from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import REGISTRY, ServiceType

# Maps UserConfiguration field name → DB service_type string → ServiceType enum
_SECTION_MAP: list[tuple[str, str, ServiceType]] = [
    ("llm", "llm", ServiceType.LLM),
    ("tts", "tts", ServiceType.TTS),
    ("stt", "stt", ServiceType.STT),
    ("realtime", "realtime", ServiceType.REALTIME),
]


async def resolve_org_provider_config(
    organization_id: int,
    base_config: UserConfiguration,
) -> UserConfiguration:
    """Fill None sections in base_config using the org's default provider connections.

    For each service type (llm, tts, stt, realtime) that is None in base_config,
    queries OrgProviderConnectionModel for an active connection, finds its default
    model, and constructs a typed config object using the service registry.

    The original base_config is never mutated.
    """
    effective = base_config.model_copy(deep=True)

    for section_key, service_type_str, service_type_enum in _SECTION_MAP:
        if getattr(effective, section_key) is not None:
            continue  # already configured — don't overwrite

        connections = await db_client.list_connections(organization_id, service_type_str)
        if not connections:
            continue

        # Use the first active connection (lowest id = oldest = implicit default)
        conn = connections[0]

        # Find the default model for this connection
        all_models = await db_client.list_available_models(
            organization_id, service_type=service_type_str
        )
        conn_models = [m for m in all_models if m.connection_id == conn.id]
        if not conn_models:
            continue
        default_model = next((m for m in conn_models if m.is_default), conn_models[0])

        # Look up the typed config class from the registry
        registry = REGISTRY.get(service_type_enum, {})
        config_cls = registry.get(conn.provider)
        if config_cls is None:
            logger.warning(
                f"resolve_org_provider_config: no registry entry for "
                f"{service_type_str}/{conn.provider} — skipping"
            )
            continue

        try:
            config_dict: dict = {
                "provider": conn.provider,
                "model": default_model.model_id,
                "api_key": conn.api_key,
                **(conn.extra_config or {}),
            }
            # TTS: if voice is absent, fall back to model_id (works for Deepgram,
            # OpenAI TTS; for ElevenLabs/Cartesia the caller must set voice separately)
            if service_type_str == "tts" and "voice" not in config_dict:
                config_dict["voice"] = default_model.model_id

            setattr(effective, section_key, config_cls(**config_dict))
        except Exception as exc:
            logger.warning(
                f"resolve_org_provider_config: failed to build config for "
                f"{section_key}/{conn.provider}: {exc}"
            )

    return effective


_SECRET_FIELDS = ("api_key", "credentials", "aws_access_key", "aws_secret_key")


async def enrich_overrides_with_org_api_keys(
    model_overrides: dict | None,
    organization_id: int,
) -> dict:
    """Stamp missing API keys (and extra_config secrets) into model_overrides from org connections.

    Called after enrich_overrides_with_api_keys (which copies from personal user config).
    This second pass fills in any sections that still lack an api_key by looking up
    the OrgProviderConnectionModel row for the given provider.
    """
    if not model_overrides:
        return model_overrides or {}

    result = copy.deepcopy(model_overrides)

    for section_key, service_type_str, _ in _SECTION_MAP:
        if section_key not in result:
            continue
        override = result[section_key]
        provider = override.get("provider")
        if not provider:
            continue

        # Only look up org connection when api_key is missing — if it's already
        # set we consider the override fully credentialled and skip the lookup.
        if override.get("api_key"):
            continue

        conn = await db_client.get_connection_by_provider(
            organization_id, service_type_str, provider
        )
        if conn is None or not conn.api_key:
            # Fallback: search all service types for the same provider.
            # Many providers (xAI, OpenAI, etc.) use one API key across all services,
            # so if the TTS connection lacks a key but the LLM connection has one, use it.
            all_conns = await db_client.list_connections(organization_id)
            conn = next(
                (c for c in all_conns if c.provider == provider and c.api_key),
                conn,
            )
            if conn is None:
                continue

        if conn.api_key:
            override["api_key"] = conn.api_key

        for k, v in (conn.extra_config or {}).items():
            if k in _SECRET_FIELDS and not override.get(k):
                override[k] = v

    return result


async def resolve_voice_for_tts(
    voice_uuid: str,
    organization_id: int,
) -> str | None:
    """Resolve a voice library UUID to the provider_voice_id string used by the TTS API.

    Returns None if the entry is not found or has no provider_voice_id set.
    """
    voice = await db_client.get_voice_by_uuid(voice_uuid, organization_id)
    if voice is None:
        return None
    return voice.provider_voice_id or None
