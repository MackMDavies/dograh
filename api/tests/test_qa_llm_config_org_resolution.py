"""QA analysis must resolve its LLM key the same way a live call does.

Regression: QA read ONLY the personal UserConfiguration.llm.api_key. On accounts
where the key comes from the org's provider connection (or run model_overrides),
that field is empty — so every analysed call failed with {"error": "no_api_key"}
even though the call itself ran fine on the org key.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import OpenAILLMService


def _workflow_run(*, org_id=42, model_overrides=None):
    run = MagicMock()
    run.workflow = MagicMock()
    run.workflow.user = MagicMock()
    run.workflow.user.id = 1
    run.workflow.organization_id = org_id
    run.definition = MagicMock()
    run.definition.workflow_configurations = (
        {"model_overrides": model_overrides} if model_overrides else {}
    )
    return run


@pytest.mark.asyncio
async def test_falls_back_to_org_connection_key_when_personal_key_empty():
    from api.services.workflow.qa.llm_config import resolve_user_llm_config

    personal = UserConfiguration(llm=None, tts=None, stt=None)
    org_resolved = UserConfiguration(
        llm=OpenAILLMService(provider="openai", model="gpt-4.1", api_key="sk-org-key"),
        tts=None,
        stt=None,
    )

    with (
        patch("api.services.workflow.qa.llm_config.db_client") as mock_db,
        patch(
            "api.services.workflow.qa.llm_config.resolve_org_provider_config",
            new=AsyncMock(return_value=org_resolved),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(return_value=personal)
        provider, model, api_key, _ = await resolve_user_llm_config(_workflow_run())

    assert api_key == "sk-org-key"
    assert provider == "openai"
    assert model == "gpt-4.1"


@pytest.mark.asyncio
async def test_personal_key_still_wins_when_present():
    from api.services.workflow.qa.llm_config import resolve_user_llm_config

    personal = UserConfiguration(
        llm=OpenAILLMService(provider="openai", model="gpt-4o", api_key="sk-personal"),
        tts=None,
        stt=None,
    )

    with (
        patch("api.services.workflow.qa.llm_config.db_client") as mock_db,
        patch(
            "api.services.workflow.qa.llm_config.resolve_org_provider_config",
            new=AsyncMock(side_effect=lambda _org, cfg: cfg),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(return_value=personal)
        _, model, api_key, _ = await resolve_user_llm_config(_workflow_run())

    assert api_key == "sk-personal"
    assert model == "gpt-4o"


@pytest.mark.asyncio
async def test_no_org_id_does_not_crash_and_uses_personal_config():
    from api.services.workflow.qa.llm_config import resolve_user_llm_config

    personal = UserConfiguration(
        llm=OpenAILLMService(provider="openai", model="gpt-4o", api_key="sk-personal"),
        tts=None,
        stt=None,
    )

    with patch("api.services.workflow.qa.llm_config.db_client") as mock_db:
        mock_db.get_user_configurations = AsyncMock(return_value=personal)
        _, _, api_key, _ = await resolve_user_llm_config(_workflow_run(org_id=None))

    assert api_key == "sk-personal"


@pytest.mark.asyncio
async def test_org_resolution_failure_does_not_break_analysis():
    """A provider-lookup failure must degrade to the personal config, not raise —
    analysis is a post-call job and must never take down the pipeline."""
    from api.services.workflow.qa.llm_config import resolve_user_llm_config

    personal = UserConfiguration(
        llm=OpenAILLMService(provider="openai", model="gpt-4o", api_key="sk-personal"),
        tts=None,
        stt=None,
    )

    with (
        patch("api.services.workflow.qa.llm_config.db_client") as mock_db,
        patch(
            "api.services.workflow.qa.llm_config.resolve_org_provider_config",
            new=AsyncMock(side_effect=RuntimeError("provider lookup down")),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(return_value=personal)
        _, _, api_key, _ = await resolve_user_llm_config(_workflow_run())

    assert api_key == "sk-personal"
