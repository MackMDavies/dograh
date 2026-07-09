from __future__ import annotations

"""Utilities for building default service configurations for a new user.

The defaults follow the same provider choices exposed by `/user/configurations/defaults`.
Values for `api_key` are pulled from environment variables named *{PROVIDER}_API_KEY*.

If an environment variable is missing, that particular provider configuration is
left as ``None``.
"""


from api.services.configuration.registry import (
    ElevenlabsTTSConfiguration,
    OpenAIEmbeddingsConfiguration,
    OpenAILLMService,
    OpenAISTTConfiguration,
    ServiceProviders,
)

# Mapping of service to (provider enum, configuration class)
_DEFAULTS = {
    "llm": (ServiceProviders.OPENAI, OpenAILLMService),
    "tts": (ServiceProviders.ELEVENLABS, ElevenlabsTTSConfiguration),
    # Default transcriber: OpenAI gpt-4o-transcribe — markedly stronger on accents,
    # noisy audio and multilingual/code-switching than Deepgram Nova-3, and reuses
    # the OpenAI API key already required for the default LLM/embeddings. Clients can
    # still switch to any other provider (Deepgram/Speechmatics/etc.) per agent.
    "stt": (ServiceProviders.OPENAI, OpenAISTTConfiguration),
    "embeddings": (ServiceProviders.OPENAI, OpenAIEmbeddingsConfiguration),
}

# Public mapping of service name -> default provider
DEFAULT_SERVICE_PROVIDERS = {
    field: provider for field, (provider, _) in _DEFAULTS.items()
}

__all__ = [
    "DEFAULT_SERVICE_PROVIDERS",
]
