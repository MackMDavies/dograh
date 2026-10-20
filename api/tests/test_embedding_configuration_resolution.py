"""Live tuple parity and provenance for every existing embedding fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.db.provider_connection_client import ProviderConnectionClient
from api.schemas.user_configuration import UserConfiguration
from api.services.gen_ai.embedding.resolver import (
    resolve_embedding_configuration_details,
    resolve_embeddings_config,
)
from api.tests.test_voice_test_providers import fixture


def connection(provider, key, *, org=42):
    return SimpleNamespace(
        provider=provider,
        api_key=key,
        organization_id=org,
        extra_config={
            "model": "selected-embedding-model",
            "base_url": "https://qa.invalid/v1",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
@pytest.mark.parametrize("tier", range(7))
async def test_metadata_and_live_tuple_follow_identical_fallbacks(
    monkeypatch, tier, provider
):
    import api.db as module
    from api.services.gen_ai.embedding import openai_service

    source = UserConfiguration()
    if tier == 0:
        source.embeddings = fixture("embeddings", provider)[0]
    before = source.model_dump()
    default_values = {
        "api_key": "qa-default-key",
        "model": "default-model",
        "base_url": "https://qa.invalid/v1",
        "provider": provider,
        "source": "org_default",
    }

    async def default(org, **kwargs):
        assert org == 42
        if tier > 1:
            return None
        return (
            default_values
            if kwargs.get("include_metadata")
            else (
                default_values["api_key"],
                default_values["model"],
                default_values["base_url"],
            )
        )

    async def org_connections(org, service_type=None):
        assert org == 42
        if service_type == "embeddings":
            return [connection(provider, "qa-org-embedding-key")] if tier <= 2 else []
        return [connection(provider, "qa-org-shared-key")] if tier <= 3 else []

    async def platform_connections(service_type=None):
        if service_type == "embeddings":
            return (
                [connection(provider, "qa-platform-embedding-key", org=1)]
                if tier <= 4
                else []
            )
        return (
            [connection(provider, "qa-platform-shared-key", org=1)] if tier <= 5 else []
        )

    db = SimpleNamespace(
        get_default_embedding_model_connection=AsyncMock(side_effect=default),
        list_connections=AsyncMock(side_effect=org_connections),
        list_all_connections_superuser=AsyncMock(side_effect=platform_connections),
    )
    monkeypatch.setattr(module, "db_client", db)
    sdk = Mock(side_effect=AssertionError("planning must not initialize a provider"))
    monkeypatch.setattr(openai_service, "AsyncOpenAI", sdk)
    live = await resolve_embeddings_config(42, source)
    metadata = await resolve_embedding_configuration_details(42, source, db=db)
    assert live == metadata.as_tuple()
    assert (
        metadata.source
        == [
            "personal",
            "org_default",
            "org_embedding",
            "org_shared_key",
            "platform_embedding",
            "platform_shared_key",
            "unconfigured",
        ][tier]
    )
    assert metadata.provider == (None if tier == 6 else provider)
    assert source.model_dump() == before
    assert "qa-" not in repr(metadata)
    if tier == 0:
        db.get_default_embedding_model_connection.assert_not_awaited()
    else:
        assert db.get_default_embedding_model_connection.await_args_list[0].kwargs == {}
        assert db.get_default_embedding_model_connection.await_args_list[1].kwargs == {
            "include_metadata": True
        }
    sdk.assert_not_called()


def database_stub(*rows):
    session = AsyncMock()
    session.execute.side_effect = [
        Mock(scalar_one_or_none=Mock(return_value=row)) for row in rows
    ]
    context = AsyncMock()
    context.__aenter__.return_value = session
    return SimpleNamespace(async_session=Mock(return_value=context)), session


@pytest.mark.asyncio
async def test_default_metadata_uses_real_dal_and_checks_connection_organization_first():
    model = SimpleNamespace(connection_id=8, model_id="chosen-model")

    class ForeignConnection:
        organization_id = 99

        @property
        def api_key(self):
            raise AssertionError(
                "must validate scope before reading a foreign credential"
            )

    db, session = database_stub(model, ForeignConnection())
    with pytest.raises(ValueError, match="^embedding_connection_scope_changed$"):
        await ProviderConnectionClient.get_default_embedding_model_connection(
            db, 42, include_metadata=True
        )
    query = str(session.execute.await_args_list[0].args[0])
    assert "org_available_models.organization_id =" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [False, True])
async def test_default_metadata_and_legacy_tuple_preserve_real_dal_model_and_endpoint(
    metadata,
):
    model = SimpleNamespace(connection_id=8, model_id="chosen-model")
    conn = connection("openrouter", "qa-key")
    db, _ = database_stub(model, conn)
    result = await ProviderConnectionClient.get_default_embedding_model_connection(
        db, 42, include_metadata=metadata
    )
    if metadata:
        assert result == {
            "api_key": "qa-key",
            "model": "chosen-model",
            "base_url": "https://qa.invalid/v1",
            "provider": "openrouter",
            "source": "org_default",
        }
    else:
        assert result == ("qa-key", "chosen-model", "https://qa.invalid/v1")
