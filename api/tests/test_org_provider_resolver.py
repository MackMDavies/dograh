"""Unit tests for org_provider_resolver functions."""
import copy
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import (
    OpenAILLMService,
    DeepgramSTTConfiguration,
    ElevenlabsTTSConfiguration,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_conn(provider, service_type, api_key, extra_config=None):
    conn = MagicMock()
    conn.id = 1
    conn.provider = provider
    conn.service_type = service_type
    conn.api_key = api_key
    conn.extra_config = extra_config or {}
    conn.is_active = True
    return conn


def _make_model(connection_id, model_id, is_default=False):
    m = MagicMock()
    m.id = 1
    m.connection_id = connection_id
    m.model_id = model_id
    m.is_default = is_default
    return m


def _make_voice(uuid, provider_voice_id, provider="elevenlabs"):
    v = MagicMock()
    v.uuid = uuid
    v.provider = provider
    v.provider_voice_id = provider_voice_id
    return v


# ── Tests: resolve_org_provider_config ───────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_org_fills_none_llm_section():
    """When user_config.llm is None, it is filled from the org's default LLM connection."""
    from api.services.configuration.org_provider_resolver import resolve_org_provider_config

    base = UserConfiguration(llm=None, tts=None, stt=None)
    conn = _make_conn("openai", "llm", "sk-org-key")
    model = _make_model(conn.id, "gpt-4.1", is_default=True)

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.list_connections = AsyncMock(return_value=[conn])
        mock_db.list_available_models = AsyncMock(return_value=[model])

        result = await resolve_org_provider_config(42, base)

    assert result.llm is not None
    assert result.llm.provider == "openai"
    assert result.llm.model == "gpt-4.1"
    assert result.llm.api_key == "sk-org-key"


@pytest.mark.asyncio
async def test_resolve_org_does_not_overwrite_existing_section():
    """When user_config.llm is already set, it is NOT replaced by org defaults."""
    from api.services.configuration.org_provider_resolver import resolve_org_provider_config

    existing_llm = OpenAILLMService(provider="openai", api_key="sk-personal", model="gpt-4.1-mini")
    base = UserConfiguration(llm=existing_llm, tts=None, stt=None)

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.list_connections = AsyncMock(return_value=[])
        mock_db.list_available_models = AsyncMock(return_value=[])

        result = await resolve_org_provider_config(42, base)

    assert result.llm.api_key == "sk-personal"
    assert result.llm.model == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_resolve_org_fills_stt_section():
    """STT section is filled from org connection when None."""
    from api.services.configuration.org_provider_resolver import resolve_org_provider_config

    base = UserConfiguration(llm=None, tts=None, stt=None)
    stt_conn = _make_conn("deepgram", "stt", "dg-org-key")
    stt_model = _make_model(stt_conn.id, "nova-3", is_default=True)

    def _list_connections(org_id, service_type=None):
        if service_type == "stt":
            return [stt_conn]
        return []

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.list_connections = AsyncMock(side_effect=_list_connections)
        mock_db.list_available_models = AsyncMock(return_value=[stt_model])

        result = await resolve_org_provider_config(42, base)

    assert result.stt is not None
    assert result.stt.provider == "deepgram"
    assert result.stt.model == "nova-3"


# ── Tests: enrich_overrides_with_org_api_keys ────────────────────────────────

@pytest.mark.asyncio
async def test_enrich_fills_missing_api_key_from_org_connection():
    """When override has provider+model but no api_key, fills it from the org connection."""
    from api.services.configuration.org_provider_resolver import enrich_overrides_with_org_api_keys

    overrides = {"llm": {"provider": "openai", "model": "gpt-4.1"}}
    conn = _make_conn("openai", "llm", "sk-org-llm")

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.get_connection_by_provider = AsyncMock(return_value=conn)

        result = await enrich_overrides_with_org_api_keys(overrides, 42)

    assert result["llm"]["api_key"] == "sk-org-llm"


@pytest.mark.asyncio
async def test_enrich_does_not_overwrite_existing_api_key():
    """If override already has an api_key, it is not overwritten."""
    from api.services.configuration.org_provider_resolver import enrich_overrides_with_org_api_keys

    overrides = {"llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "sk-already-set"}}

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.get_connection_by_provider = AsyncMock()

        result = await enrich_overrides_with_org_api_keys(overrides, 42)

    mock_db.get_connection_by_provider.assert_not_called()
    assert result["llm"]["api_key"] == "sk-already-set"


# ── Tests: resolve_voice_for_tts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_voice_returns_provider_voice_id():
    """Given a voice_uuid, returns the matching provider_voice_id."""
    from api.services.configuration.org_provider_resolver import resolve_voice_for_tts

    voice = _make_voice("abc-123", "21m00Tcm4TlvDq8ikWAM")

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.get_voice_by_uuid = AsyncMock(return_value=voice)

        result = await resolve_voice_for_tts("abc-123", 42)

    assert result == "21m00Tcm4TlvDq8ikWAM"


@pytest.mark.asyncio
async def test_resolve_voice_returns_none_when_not_found():
    """Returns None when voice_uuid has no matching library entry."""
    from api.services.configuration.org_provider_resolver import resolve_voice_for_tts

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.get_voice_by_uuid = AsyncMock(return_value=None)

        result = await resolve_voice_for_tts("nonexistent", 42)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_voice_for_tts_with_no_provider_voice_id():
    """Returns None when voice entry exists but has no provider_voice_id."""
    from api.services.configuration.org_provider_resolver import resolve_voice_for_tts

    voice = _make_voice("abc-123", None)  # provider_voice_id = None

    with patch("api.services.configuration.org_provider_resolver.db_client") as mock_db:
        mock_db.get_voice_by_uuid = AsyncMock(return_value=voice)

        result = await resolve_voice_for_tts("abc-123", 42)

    assert result is None
