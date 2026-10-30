"""Configuration planning must not copy validation input into logs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.db.user_client import UserClient
from api.schemas.user_configuration import UserConfiguration
from api.services.configuration import org_provider_resolver

PRIVATE = "qa-only-729"


@pytest.mark.asyncio
async def test_invalid_personal_configuration_does_not_log_validation_input(
    monkeypatch,
):
    import api.db.user_client as module

    row = SimpleNamespace(
        configuration={"llm": {"provider": "openai", "api_key": {"invalid": PRIVATE}}},
        last_validated_at=None,
    )
    session = AsyncMock()
    session.execute.return_value = Mock(
        scalars=Mock(return_value=Mock(first=Mock(return_value=row)))
    )
    context = AsyncMock()
    context.__aenter__.return_value = session
    db = SimpleNamespace(async_session=Mock(return_value=context))
    logger = Mock()
    monkeypatch.setattr(module, "logger", logger)
    result = await UserClient.get_user_configurations(db, 7)
    assert result == UserConfiguration()
    assert logger.warning.called
    assert PRIVATE not in str(logger.warning.call_args_list)


@pytest.mark.asyncio
async def test_invalid_org_configuration_does_not_log_validation_input(monkeypatch):
    connection = SimpleNamespace(
        id=1,
        organization_id=42,
        provider="openai",
        api_key=PRIVATE,
        extra_config={"model": {"invalid": PRIVATE}},
    )
    model = SimpleNamespace(connection_id=1, is_default=True, model_id="model")
    db = SimpleNamespace(
        list_connections=AsyncMock(return_value=[connection]),
        list_available_models=AsyncMock(return_value=[model]),
    )
    logger = Mock()
    monkeypatch.setattr(org_provider_resolver, "db_client", db)
    monkeypatch.setattr(org_provider_resolver, "logger", logger)
    result = await org_provider_resolver.resolve_org_provider_config(
        42, UserConfiguration()
    )
    assert result.llm is None
    assert logger.warning.called
    assert PRIVATE not in str(logger.warning.call_args_list)
