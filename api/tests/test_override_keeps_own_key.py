"""An override pointing at its OWN endpoint keeps its own key when the workflow is saved.

Found 2026-09-23: Sysevo's Syra voice workflow (llm.base_url = syra-voice-llm, api_key =
SYRA_VOICE_SECRET) had the org's OpenAI key stamped over its own on every save, sent it to
syra-voice-llm and got 401 on every turn. The three cases below pin the fix and pin that
nothing else changed.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.configuration.org_provider_resolver import enrich_overrides_with_org_api_keys


def _conn(provider, api_key):
    c = MagicMock()
    c.provider = provider
    c.api_key = api_key
    c.extra_config = {}
    return c


def _db(conn):
    db = MagicMock()
    db.get_connection_by_provider = AsyncMock(return_value=conn)
    db.list_connections = AsyncMock(return_value=[conn])
    db.list_all_connections_superuser = AsyncMock(return_value=[conn])
    return db


@pytest.mark.asyncio
async def test_own_endpoint_keeps_its_own_real_key():
    overrides = {"llm": {"provider": "openai", "model": "syra", "base_url": "https://x.supabase.co/functions/v1/syra-voice-llm", "api_key": "syra-secret"}}
    with patch("api.services.configuration.org_provider_resolver.db_client", _db(_conn("openai", "sk-proj-org"))):
        out = await enrich_overrides_with_org_api_keys(overrides, 1)
    assert out["llm"]["api_key"] == "syra-secret"


@pytest.mark.asyncio
async def test_ordinary_override_is_still_restamped_from_the_org():
    overrides = {"llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "sk-proj-old"}}
    with patch("api.services.configuration.org_provider_resolver.db_client", _db(_conn("openai", "sk-proj-rotated"))):
        out = await enrich_overrides_with_org_api_keys(overrides, 1)
    assert out["llm"]["api_key"] == "sk-proj-rotated"


@pytest.mark.asyncio
async def test_missing_key_is_still_filled():
    overrides = {"llm": {"provider": "openai", "model": "gpt-4.1", "base_url": "https://example.com/v1"}}
    with patch("api.services.configuration.org_provider_resolver.db_client", _db(_conn("openai", "sk-proj-org"))):
        out = await enrich_overrides_with_org_api_keys(overrides, 1)
    assert out["llm"]["api_key"] == "sk-proj-org"
