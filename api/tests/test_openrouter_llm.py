"""OpenRouter LLM wiring and credential validation.

OpenRouter escrows credits against the max_tokens a request declares. When
max_tokens is omitted it reserves the model's entire context window, which
rejects the call with a 402 on any account without a large balance. The
service must therefore always declare a bound, and validation must exercise
the same bound so an unaffordable config fails at save time rather than
mid-call.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    OPENROUTER_DEFAULT_MAX_TOKENS,
    ServiceProviders,
)
from api.services.pipecat.service_factory import create_llm_service_from_provider


def _openrouter_config(model="openai/gpt-4.1", api_key="sk-or-v1-test", base_url=None):
    return SimpleNamespace(
        provider=ServiceProviders.OPENROUTER.value,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def _api_status_error(status_code, message):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return openai.APIStatusError(message, response=response, body=None)


# ── service wiring ────────────────────────────────────────────────────────


def test_openrouter_llm_service_declares_max_tokens():
    with patch(
        "api.services.pipecat.service_factory.OpenRouterLLMService"
    ) as mock_service:
        create_llm_service_from_provider(
            ServiceProviders.OPENROUTER.value,
            "openai/gpt-4.1",
            "sk-or-v1-test",
        )

    settings = mock_service.call_args.kwargs["settings"]
    assert settings.max_tokens == OPENROUTER_DEFAULT_MAX_TOKENS


def test_openai_llm_service_does_not_declare_max_tokens():
    """The bound is OpenRouter-specific; direct OpenAI must be untouched."""
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_service:
        create_llm_service_from_provider(
            ServiceProviders.OPENAI.value,
            "gpt-4.1",
            "sk-test",
        )

    settings = mock_service.call_args.kwargs["settings"]
    assert not isinstance(settings.max_tokens, int)


# ── validation ────────────────────────────────────────────────────────────


def test_validation_passes_on_successful_completion():
    validator = UserConfigurationValidator()
    client = MagicMock()

    with patch("openai.OpenAI", return_value=client):
        assert validator._check_openrouter_api_key(
            ServiceProviders.OPENROUTER.value, "sk-or-v1-test", _openrouter_config()
        )

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4.1"
    assert kwargs["max_tokens"] == OPENROUTER_DEFAULT_MAX_TOKENS


def test_validation_reports_insufficient_credits():
    validator = UserConfigurationValidator()
    client = MagicMock()
    client.chat.completions.create.side_effect = _api_status_error(
        402, "This request requires more credits, or fewer max_tokens."
    )

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(ValueError, match="credits"):
            validator._check_openrouter_api_key(
                ServiceProviders.OPENROUTER.value, "sk-or-v1-test", _openrouter_config()
            )


def test_validation_reports_unknown_model_slug():
    validator = UserConfigurationValidator()
    client = MagicMock()
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    client.chat.completions.create.side_effect = openai.NotFoundError(
        "No endpoints found",
        response=httpx.Response(404, request=request),
        body=None,
    )

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(ValueError, match="deepseek/deepseek-chat-v3-0324:free"):
            validator._check_openrouter_api_key(
                ServiceProviders.OPENROUTER.value,
                "sk-or-v1-test",
                _openrouter_config(model="deepseek/deepseek-chat-v3-0324:free"),
            )


def test_validation_reports_rejected_key():
    validator = UserConfigurationValidator()
    client = MagicMock()
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    client.chat.completions.create.side_effect = openai.AuthenticationError(
        "No auth credentials found",
        response=httpx.Response(401, request=request),
        body=None,
    )

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(ValueError, match="rejected"):
            validator._check_openrouter_api_key(
                ServiceProviders.OPENROUTER.value, "sk-or-v1-test", _openrouter_config()
            )


def test_validation_routes_service_config_to_openrouter_validator():
    """_check_api_key must forward service_config so the model slug is checked."""
    validator = UserConfigurationValidator()
    config = _openrouter_config()

    # The map captures bound methods at __init__, so patch the entry itself.
    mock_check = MagicMock(return_value=True)
    validator._validator_map[ServiceProviders.OPENROUTER.value] = mock_check

    validator._check_api_key(ServiceProviders.OPENROUTER.value, "sk-or-v1-test", config)

    assert mock_check.call_args.args[2] is config
