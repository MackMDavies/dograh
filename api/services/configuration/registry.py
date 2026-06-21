import random
from enum import Enum, auto
from typing import Annotated, Dict, Literal, Type, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from api.services.configuration.options import (
    DEEPGRAM_LANGUAGES,
    DEEPGRAM_STT_MODELS,
    GLADIA_STT_LANGUAGES,
    GLADIA_STT_MODELS,
    GOOGLE_MODELS,
    GOOGLE_REALTIME_LANGUAGES,
    GOOGLE_REALTIME_MODELS,
    GOOGLE_REALTIME_VOICES,
    GOOGLE_STT_LANGUAGES,
    GOOGLE_STT_MODELS,
    GOOGLE_TTS_LANGUAGES,
    GOOGLE_TTS_MODELS,
    GOOGLE_TTS_VOICES,
    GOOGLE_VERTEX_REALTIME_LANGUAGES,
    GOOGLE_VERTEX_REALTIME_MODELS,
    GOOGLE_VERTEX_REALTIME_VOICES,
    SARVAM_LANGUAGES,
    SARVAM_STT_MODELS,
    SARVAM_TTS_MODELS,
    SARVAM_V2_VOICES,
    SARVAM_V3_VOICES,
    SPEECHMATICS_STT_LANGUAGES,
)
from api.services.configuration.options.google import GOOGLE_VERTEX_MODELS


class ServiceType(Enum):
    LLM = auto()
    TTS = auto()
    STT = auto()
    EMBEDDINGS = auto()
    REALTIME = auto()


class ServiceProviders(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPGRAM = "deepgram"
    GROQ = "groq"
    XAI = "xai"
    OPENROUTER = "openrouter"
    CARTESIA = "cartesia"
    # NEUPHONIC = "neuphonic"
    ELEVENLABS = "elevenlabs"
    GOOGLE = "google"
    AZURE = "azure"
    DOGRAH = "dograh"
    SARVAM = "sarvam"
    SPEECHMATICS = "speechmatics"
    CAMB = "camb"
    AWS_BEDROCK = "aws_bedrock"
    SPEACHES = "speaches"
    ASSEMBLYAI = "assemblyai"
    GLADIA = "gladia"
    RIME = "rime"
    MINIMAX = "minimax"
    GOOGLE_VERTEX = "google_vertex"
    OPENAI_REALTIME = "openai_realtime"
    GROK_REALTIME = "grok_realtime"
    ULTRAVOX_REALTIME = "ultravox_realtime"
    GOOGLE_REALTIME = "google_realtime"
    GOOGLE_VERTEX_REALTIME = "google_vertex_realtime"
    # ── New LLM providers ─────────────────────────────────────────────────────
    MISTRAL = "mistral"
    TOGETHER = "together"
    CEREBRAS = "cerebras"
    FIREWORKS = "fireworks"
    COHERE = "cohere"
    # ── New TTS providers ─────────────────────────────────────────────────────
    AWS_POLLY = "aws_polly"
    AZURE_TTS = "azure_tts"
    PLAYHT = "playht"
    NEETS = "neets"
    # ── New STT providers ─────────────────────────────────────────────────────
    AZURE_SPEECH = "azure_speech"
    AWS_TRANSCRIBE = "aws_transcribe"


class BaseServiceConfiguration(BaseModel):
    provider: Literal[
        ServiceProviders.OPENAI,
        ServiceProviders.ANTHROPIC,
        ServiceProviders.DEEPGRAM,
        ServiceProviders.GROQ,
        ServiceProviders.XAI,
        ServiceProviders.OPENROUTER,
        ServiceProviders.ELEVENLABS,
        ServiceProviders.GOOGLE,
        ServiceProviders.AZURE,
        ServiceProviders.DOGRAH,
        ServiceProviders.AWS_BEDROCK,
        ServiceProviders.SPEACHES,
        ServiceProviders.ASSEMBLYAI,
        ServiceProviders.GLADIA,
        ServiceProviders.RIME,
        ServiceProviders.MINIMAX,
        ServiceProviders.GOOGLE_VERTEX,
        ServiceProviders.OPENAI_REALTIME,
        ServiceProviders.GROK_REALTIME,
        ServiceProviders.ULTRAVOX_REALTIME,
        ServiceProviders.GOOGLE_REALTIME,
        ServiceProviders.GOOGLE_VERTEX_REALTIME,
        ServiceProviders.MISTRAL,
        ServiceProviders.TOGETHER,
        ServiceProviders.CEREBRAS,
        ServiceProviders.FIREWORKS,
        ServiceProviders.COHERE,
        ServiceProviders.AWS_POLLY,
        ServiceProviders.AZURE_TTS,
        ServiceProviders.PLAYHT,
        ServiceProviders.NEETS,
        ServiceProviders.AZURE_SPEECH,
        ServiceProviders.AWS_TRANSCRIBE,
        # ServiceProviders.SARVAM,
    ]
    api_key: str | list[str]

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v):
        if v is None:
            return v
        if isinstance(v, list) and len(v) == 0:
            raise ValueError("api_key list must not be empty")
        return v

    def __getattribute__(self, name: str):
        if name == "api_key":
            value = super().__getattribute__(name)
            if value is None:
                return value
            if isinstance(value, list):
                return random.choice(value)
            return value
        return super().__getattribute__(name)

    def get_all_api_keys(self) -> list[str]:
        """Get all API keys as a list (bypasses random selection)."""
        value = super().__getattribute__("api_key")
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        return [value]


class BaseLLMConfiguration(BaseServiceConfiguration):
    model: str


class BaseTTSConfiguration(BaseServiceConfiguration):
    model: str


class BaseSTTConfiguration(BaseServiceConfiguration):
    model: str


class BaseEmbeddingsConfiguration(BaseServiceConfiguration):
    model: str


# Unified registry for all service types
REGISTRY: Dict[ServiceType, Dict[str, Type[BaseServiceConfiguration]]] = {
    ServiceType.LLM: {},
    ServiceType.TTS: {},
    ServiceType.STT: {},
    ServiceType.EMBEDDINGS: {},
    ServiceType.REALTIME: {},
}

T = TypeVar("T", bound=BaseServiceConfiguration)


def register_service(service_type: ServiceType):
    """Generic decorator for registering service configurations"""

    def decorator(cls: Type[T]) -> Type[T]:
        # Get provider from class attributes or field defaults
        provider = getattr(cls, "provider", None)
        if provider is None:
            # Try to get from model fields
            provider = cls.model_fields.get("provider", None)
            if provider is not None:
                provider = provider.default
        if provider is None:
            raise ValueError(f"Provider not specified for {cls.__name__}")

        REGISTRY[service_type][provider] = cls
        return cls

    return decorator


# Convenience decorators
def register_llm(cls: Type[BaseLLMConfiguration]):
    return register_service(ServiceType.LLM)(cls)


def register_tts(cls: Type[BaseTTSConfiguration]):
    return register_service(ServiceType.TTS)(cls)


def register_stt(cls: Type[BaseSTTConfiguration]):
    return register_service(ServiceType.STT)(cls)


def register_embeddings(cls: Type[BaseEmbeddingsConfiguration]):
    return register_service(ServiceType.EMBEDDINGS)(cls)


def provider_model_config(
    title: str,
    *,
    description: str | None = None,
    provider_docs_url: str | None = None,
) -> ConfigDict:
    json_schema_extra: dict[str, str] = {}
    if description is not None:
        json_schema_extra["description"] = description
    if provider_docs_url is not None:
        json_schema_extra["provider_docs_url"] = provider_docs_url
    if json_schema_extra:
        return ConfigDict(title=title, json_schema_extra=json_schema_extra)
    return ConfigDict(title=title)


###################################################### LLM ########################################################################

# Suggested models for each provider (used for UI dropdown)
OPENAI_PROVIDER_MODEL_CONFIG = provider_model_config("OpenAI")
ANTHROPIC_PROVIDER_MODEL_CONFIG = provider_model_config("Anthropic")
GOOGLE_PROVIDER_MODEL_CONFIG = provider_model_config("Google")
GROQ_PROVIDER_MODEL_CONFIG = provider_model_config("Groq")
XAI_PROVIDER_MODEL_CONFIG = provider_model_config(
    "xAI",
    description="xAI's Grok models via the OpenAI-compatible API at api.x.ai.",
    provider_docs_url="https://docs.x.ai/docs",
)
OPENROUTER_PROVIDER_MODEL_CONFIG = provider_model_config("Open Router")
AZURE_OPENAI_PROVIDER_MODEL_CONFIG = provider_model_config("Azure OpenAI")
DOGRAH_PROVIDER_MODEL_CONFIG = provider_model_config("Dograh")
AWS_BEDROCK_PROVIDER_MODEL_CONFIG = provider_model_config("AWS Bedrock")
GOOGLE_VERTEX_PROVIDER_MODEL_CONFIG = provider_model_config("Google Vertex")
OPENAI_REALTIME_PROVIDER_MODEL_CONFIG = provider_model_config("OpenAI Realtime")
GROK_REALTIME_PROVIDER_MODEL_CONFIG = provider_model_config("Grok Realtime")
ULTRAVOX_REALTIME_PROVIDER_MODEL_CONFIG = provider_model_config("Ultravox Realtime")
GOOGLE_REALTIME_PROVIDER_MODEL_CONFIG = provider_model_config("Google Realtime")
GOOGLE_VERTEX_REALTIME_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Google Vertex Realtime"
)
DEEPGRAM_PROVIDER_MODEL_CONFIG = provider_model_config("Deepgram")
ELEVENLABS_PROVIDER_MODEL_CONFIG = provider_model_config("ElevenLabs")
CARTESIA_PROVIDER_MODEL_CONFIG = provider_model_config("Cartesia")
SARVAM_PROVIDER_MODEL_CONFIG = provider_model_config("Sarvam")
CAMB_PROVIDER_MODEL_CONFIG = provider_model_config("Camb.ai")
RIME_PROVIDER_MODEL_CONFIG = provider_model_config("Rime")
GOOGLE_CLOUD_PROVIDER_MODEL_CONFIG = provider_model_config("Google Cloud")
SPEECHMATICS_PROVIDER_MODEL_CONFIG = provider_model_config("Speechmatics")
ASSEMBLYAI_PROVIDER_MODEL_CONFIG = provider_model_config("AssemblyAI")
GLADIA_PROVIDER_MODEL_CONFIG = provider_model_config("Gladia")
SPEACHES_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Local Models (Speaches)",
    description=(
        "Self-hosted OpenAI-compatible local models. See the Speaches project "
        "for setup and supported backends."
    ),
    provider_docs_url="https://github.com/speaches-ai/speaches",
)

ANTHROPIC_MODELS = [
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5-20251101",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "claude-3-haiku-20240307",
]

OPENAI_MODELS = [
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-3.5-turbo",
]

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "deepseek-r1-distill-llama-70b",
    "qwen-qwq-32b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "gemma2-9b-it",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
]

XAI_MODELS = [
    "grok-3",
    "grok-3-mini",
    "grok-3-fast",
    "grok-3-mini-fast",
    "grok-2-1212",
    "grok-2-vision-1212",
]

OPENROUTER_MODELS = [
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
    "anthropic/claude-sonnet-4",
    "google/gemini-2.5-flash",
    "google/gemini-2.0-flash",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat-v3-0324",
]
AZURE_MODELS = ["gpt-4.1-mini"]
DOGRAH_LLM_MODELS = ["default", "accurate", "fast", "lite", "zen"]
AWS_BEDROCK_MODELS = [
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
]


@register_llm
class OpenAILLMService(BaseLLMConfiguration):
    model_config = OPENAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENAI] = ServiceProviders.OPENAI
    model: str = Field(
        default="gpt-4.1",
        description="OpenAI chat model to use.",
        json_schema_extra={"examples": OPENAI_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Override only if using an OpenAI-compatible API (e.g. local LLM, proxy).",
    )


@register_llm
class AnthropicLLMConfiguration(BaseLLMConfiguration):
    model_config = ANTHROPIC_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.ANTHROPIC] = ServiceProviders.ANTHROPIC
    model: str = Field(
        default="claude-sonnet-4-6",
        description="Anthropic Claude model identifier.",
        json_schema_extra={"examples": ANTHROPIC_MODELS, "allow_custom_input": True},
    )


@register_llm
class GoogleLLMService(BaseLLMConfiguration):
    model_config = GOOGLE_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GOOGLE] = ServiceProviders.GOOGLE
    model: str = Field(
        default="gemini-2.0-flash",
        description="Gemini model on Google AI Studio (not Vertex).",
        json_schema_extra={"examples": GOOGLE_MODELS, "allow_custom_input": True},
    )


@register_llm
class GoogleVertexLLMConfiguration(BaseLLMConfiguration):
    model_config = GOOGLE_VERTEX_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GOOGLE_VERTEX] = ServiceProviders.GOOGLE_VERTEX
    model: str = Field(
        default="gemini-2.5-flash",
        description="Gemini model on Vertex AI.",
        json_schema_extra={
            "examples": GOOGLE_VERTEX_MODELS,
            "allow_custom_input": True,
        },
    )
    project_id: str = Field(description="Google Cloud project ID for Vertex AI.")
    location: str = Field(
        default="global",
        description="GCP region for the Vertex AI endpoint (e.g. 'global').",
    )
    credentials: str | None = Field(
        default=None,
        description=(
            "Paste the entire service-account JSON file contents. If omitted, "
            "falls back to Application Default Credentials (ADC)."
        ),
        json_schema_extra={"multiline": True},
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description=(
            "Not used for Vertex AI — authentication is via the service account "
            "in `credentials` (or ADC). Leave blank."
        ),
    )


@register_llm
class GroqLLMService(BaseLLMConfiguration):
    model_config = GROQ_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GROQ] = ServiceProviders.GROQ
    model: str = Field(
        default="llama-3.3-70b-versatile",
        description="Groq-hosted model identifier.",
        json_schema_extra={"examples": GROQ_MODELS, "allow_custom_input": True},
    )


@register_llm
class XAILLMConfiguration(BaseLLMConfiguration):
    model_config = XAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.XAI] = ServiceProviders.XAI
    model: str = Field(
        default="grok-3",
        description="xAI Grok model identifier.",
        json_schema_extra={"examples": XAI_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.x.ai/v1",
        description="Override only if proxying through a custom gateway.",
    )


@register_llm
class OpenRouterLLMConfiguration(BaseLLMConfiguration):
    model_config = OPENROUTER_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENROUTER] = ServiceProviders.OPENROUTER
    model: str = Field(
        default="openai/gpt-4.1",
        description="OpenRouter model slug in 'vendor/model' form.",
        json_schema_extra={"examples": OPENROUTER_MODELS, "allow_custom_input": True},
    )

    base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="Override only if proxying OpenRouter through your own gateway.",
    )


@register_llm
class AzureLLMService(BaseLLMConfiguration):
    model_config = AZURE_OPENAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.AZURE] = ServiceProviders.AZURE
    model: str = Field(
        default="gpt-4.1-mini",
        description="Azure deployment name (not the upstream OpenAI model id).",
        json_schema_extra={"examples": AZURE_MODELS, "allow_custom_input": True},
    )

    endpoint: str = Field(
        description="Azure OpenAI resource endpoint (e.g. https://<resource>.openai.azure.com).",
    )


@register_llm
class DograhLLMService(BaseLLMConfiguration):
    model_config = DOGRAH_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.DOGRAH] = ServiceProviders.DOGRAH
    model: str = Field(
        default="default",
        description="Dograh-hosted model tier.",
        json_schema_extra={"examples": DOGRAH_LLM_MODELS, "allow_custom_input": True},
    )


@register_llm
class AWSBedrockLLMConfiguration(BaseLLMConfiguration):
    model_config = AWS_BEDROCK_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.AWS_BEDROCK] = ServiceProviders.AWS_BEDROCK
    model: str = Field(
        default="us.amazon.nova-pro-v1:0",
        description="Bedrock model ID — include the region inference-profile prefix (e.g. 'us.').",
        json_schema_extra={"examples": AWS_BEDROCK_MODELS, "allow_custom_input": True},
    )
    aws_access_key: str = Field(
        default="",
        description="AWS access key ID with bedrock:InvokeModel permission.",
    )
    aws_secret_key: str = Field(
        default="",
        description="AWS secret access key paired with the access key ID.",
    )
    aws_region: str = Field(
        default="us-east-1",
        description="AWS region where the Bedrock model is available.",
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Not used for Bedrock — authentication is via the AWS credentials above. Leave blank.",
    )


MISTRAL_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Mistral AI",
    description="Mistral AI models via OpenAI-compatible API. EU GDPR compliant with data residency options.",
    provider_docs_url="https://docs.mistral.ai/",
)
TOGETHER_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Together AI",
    description="100+ open-source models hosted on Together AI's inference platform.",
    provider_docs_url="https://docs.together.ai/",
)
CEREBRAS_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Cerebras",
    description="Ultra-low latency inference on Cerebras Wafer-Scale silicon.",
    provider_docs_url="https://inference-docs.cerebras.ai/",
)
FIREWORKS_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Fireworks AI",
    description="Fast and cheap open-source model inference on Fireworks AI.",
    provider_docs_url="https://docs.fireworks.ai/",
)
COHERE_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Cohere",
    description="Command models optimised for RAG, multilingual tasks, and enterprise workloads.",
    provider_docs_url="https://docs.cohere.com/",
)
AWS_POLLY_PROVIDER_MODEL_CONFIG = provider_model_config(
    "AWS Polly",
    description="Amazon Polly TTS — 60+ voices, 30+ languages, standard and neural engines.",
    provider_docs_url="https://docs.aws.amazon.com/polly/",
)
AZURE_TTS_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Azure TTS",
    description="Microsoft Azure Text-to-Speech — 400+ neural voices across 140+ languages.",
    provider_docs_url="https://learn.microsoft.com/en-us/azure/ai-services/speech-service/",
)
PLAYHT_PROVIDER_MODEL_CONFIG = provider_model_config(
    "PlayHT",
    description="PlayHT ultra-realistic TTS with voice cloning and instant custom voices.",
    provider_docs_url="https://docs.play.ht/",
)
NEETS_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Neets.ai",
    description="Ultra-low-cost TTS — among the cheapest non-self-hosted providers.",
    provider_docs_url="https://neets.ai/docs",
)
AZURE_SPEECH_PROVIDER_MODEL_CONFIG = provider_model_config(
    "Azure Speech",
    description="Microsoft Azure Speech-to-Text — real-time transcription with 100+ language support.",
    provider_docs_url="https://learn.microsoft.com/en-us/azure/ai-services/speech-service/",
)
AWS_TRANSCRIBE_PROVIDER_MODEL_CONFIG = provider_model_config(
    "AWS Transcribe",
    description="Amazon Transcribe streaming STT — accurate, low-latency transcription.",
    provider_docs_url="https://docs.aws.amazon.com/transcribe/",
)

MISTRAL_MODELS = [
    "mistral-large-latest",
    "mistral-medium-latest",
    "mistral-small-latest",
    "mistral-nemo",
    "codestral-latest",
    "open-mistral-7b",
    "open-mixtral-8x7b",
    "open-mixtral-8x22b",
]

TOGETHER_MODELS = [
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "meta-llama/Llama-3.1-405B-Instruct-Turbo",
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
    "meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo",
    "Qwen/Qwen2.5-72B-Instruct-Turbo",
    "Qwen/Qwen2.5-7B-Instruct-Turbo",
    "deepseek-ai/DeepSeek-V3",
    "mistralai/Mixtral-8x7B-Instruct-v0.1",
    "mistralai/Mistral-7B-Instruct-v0.3",
    "google/gemma-2-27b-it",
]

CEREBRAS_MODELS = [
    "llama3.3-70b",
    "llama3.1-70b",
    "llama3.1-8b",
    "llama3.1-405b",
]

FIREWORKS_MODELS = [
    "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "accounts/fireworks/models/llama-v3p1-8b-instruct",
    "accounts/fireworks/models/llama-v3p1-405b-instruct",
    "accounts/fireworks/models/mixtral-8x7b-instruct",
    "accounts/fireworks/models/qwen2p5-72b-instruct",
    "accounts/fireworks/models/deepseek-v3",
    "accounts/fireworks/models/gemma2-9b-it",
]

COHERE_MODELS = [
    "command-r-plus-08-2024",
    "command-r-08-2024",
    "command-r",
    "command-light",
    "command-r7b-12-2024",
]


@register_llm
class MistralLLMConfiguration(BaseLLMConfiguration):
    model_config = MISTRAL_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.MISTRAL] = ServiceProviders.MISTRAL
    model: str = Field(
        default="mistral-large-latest",
        description="Mistral model identifier.",
        json_schema_extra={"examples": MISTRAL_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.mistral.ai/v1",
        description="Override only if using a Mistral-compatible proxy or custom endpoint.",
    )


@register_llm
class TogetherLLMConfiguration(BaseLLMConfiguration):
    model_config = TOGETHER_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.TOGETHER] = ServiceProviders.TOGETHER
    model: str = Field(
        default="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        description="Together AI model slug.",
        json_schema_extra={"examples": TOGETHER_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.together.xyz/v1",
        description="Override only if routing through a custom Together-compatible gateway.",
    )


@register_llm
class CerebrasLLMConfiguration(BaseLLMConfiguration):
    model_config = CEREBRAS_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.CEREBRAS] = ServiceProviders.CEREBRAS
    model: str = Field(
        default="llama3.3-70b",
        description="Cerebras-hosted model identifier.",
        json_schema_extra={"examples": CEREBRAS_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.cerebras.ai/v1",
        description="Override only if using a Cerebras-compatible proxy.",
    )


@register_llm
class FireworksLLMConfiguration(BaseLLMConfiguration):
    model_config = FIREWORKS_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.FIREWORKS] = ServiceProviders.FIREWORKS
    model: str = Field(
        default="accounts/fireworks/models/llama-v3p3-70b-instruct",
        description="Fireworks AI model path (accounts/fireworks/models/...).",
        json_schema_extra={"examples": FIREWORKS_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.fireworks.ai/inference/v1",
        description="Override only if routing through a Fireworks-compatible gateway.",
    )


@register_llm
class CohereLLMConfiguration(BaseLLMConfiguration):
    model_config = COHERE_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.COHERE] = ServiceProviders.COHERE
    model: str = Field(
        default="command-r-plus-08-2024",
        description="Cohere Command model identifier.",
        json_schema_extra={"examples": COHERE_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.cohere.com/compatibility/v1",
        description="Cohere OpenAI-compatible endpoint. Override only if using a custom proxy.",
    )


SPEACHES_LLM_MODELS = ["llama3", "mistral", "phi3", "qwen2", "gemma2", "deepseek-r1"]


@register_llm
class SpeachesLLMConfiguration(BaseLLMConfiguration):
    model_config = SPEACHES_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.SPEACHES] = ServiceProviders.SPEACHES
    model: str = Field(
        default="llama3",
        description="Model name as exposed by your OpenAI-compatible server.",
        json_schema_extra={
            "examples": SPEACHES_LLM_MODELS,
            "allow_custom_input": True,
        },
    )
    base_url: str = Field(
        default="http://localhost:11434/v1",
        description="OpenAI-compatible endpoint (Ollama, vLLM, etc.).",
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Usually not required for self-hosted endpoints. Leave blank unless your server enforces one.",
    )


MINIMAX_MODELS = [
    "MiniMax-M2.7",
    "MiniMax-M2.7-highspeed",
]


@register_llm
class MiniMaxLLMConfiguration(BaseLLMConfiguration):
    provider: Literal[ServiceProviders.MINIMAX] = ServiceProviders.MINIMAX
    model: str = Field(
        default="MiniMax-M2.7",
        description="MiniMax chat model.",
        json_schema_extra={"examples": MINIMAX_MODELS, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.minimax.io/v1",
        description="MiniMax OpenAI-compatible API endpoint.",
    )
    temperature: float = Field(
        default=1.0,
        gt=0.0,
        le=2.0,
        description="Sampling temperature. MiniMax requires > 0.",
    )


OPENAI_REALTIME_MODELS = ["gpt-realtime-2"]
OPENAI_REALTIME_VOICES = [
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
]


@register_service(ServiceType.REALTIME)
class OpenAIRealtimeLLMConfiguration(BaseLLMConfiguration):
    model_config = OPENAI_REALTIME_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENAI_REALTIME] = (
        ServiceProviders.OPENAI_REALTIME
    )
    model: str = Field(
        default="gpt-realtime-2",
        description="OpenAI realtime (speech-to-speech) model.",
        json_schema_extra={
            "examples": OPENAI_REALTIME_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="alloy",
        description="Voice the model speaks in.",
        json_schema_extra={
            "examples": OPENAI_REALTIME_VOICES,
            "allow_custom_input": True,
        },
    )


GROK_REALTIME_MODELS = ["grok-voice-think-fast-1.0"]
GROK_REALTIME_VOICES = ["Ara", "Rex", "Sal", "Eve", "Leo"]
ULTRAVOX_REALTIME_MODELS = ["ultravox-v0.7", "fixie-ai/ultravox"]


@register_service(ServiceType.REALTIME)
class GrokRealtimeLLMConfiguration(BaseLLMConfiguration):
    model_config = GROK_REALTIME_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GROK_REALTIME] = ServiceProviders.GROK_REALTIME
    model: str = Field(
        default="grok-voice-think-fast-1.0",
        description="Grok realtime voice-agent model.",
        json_schema_extra={
            "examples": GROK_REALTIME_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="Ara",
        description="Voice the model speaks in.",
        json_schema_extra={
            "examples": GROK_REALTIME_VOICES,
            "allow_custom_input": True,
        },
    )


@register_service(ServiceType.REALTIME)
class UltravoxRealtimeLLMConfiguration(BaseLLMConfiguration):
    model_config = ULTRAVOX_REALTIME_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.ULTRAVOX_REALTIME] = (
        ServiceProviders.ULTRAVOX_REALTIME
    )
    model: str = Field(
        default="ultravox-v0.7",
        description="Ultravox realtime voice-agent model.",
        json_schema_extra={
            "examples": ULTRAVOX_REALTIME_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="Mark",
        description="Ultravox voice name or voice ID.",
    )


@register_service(ServiceType.REALTIME)
class GoogleRealtimeLLMConfiguration(BaseLLMConfiguration):
    model_config = GOOGLE_REALTIME_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GOOGLE_REALTIME] = (
        ServiceProviders.GOOGLE_REALTIME
    )
    model: str = Field(
        default="gemini-3.1-flash-live-preview",
        description="Gemini Live model on Google AI Studio (not Vertex).",
        json_schema_extra={
            "examples": GOOGLE_REALTIME_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="Puck",
        description="Voice the model speaks in.",
        json_schema_extra={
            "examples": GOOGLE_REALTIME_VOICES,
            "allow_custom_input": True,
        },
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
        json_schema_extra={
            "examples": GOOGLE_REALTIME_LANGUAGES,
            "allow_custom_input": True,
        },
    )


@register_service(ServiceType.REALTIME)
class GoogleVertexRealtimeLLMConfiguration(BaseLLMConfiguration):
    model_config = GOOGLE_VERTEX_REALTIME_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GOOGLE_VERTEX_REALTIME] = (
        ServiceProviders.GOOGLE_VERTEX_REALTIME
    )
    model: str = Field(
        default="google/gemini-live-2.5-flash-native-audio",
        description="Vertex AI publisher/model identifier.",
        json_schema_extra={
            "examples": GOOGLE_VERTEX_REALTIME_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="Charon",
        description="Voice the model speaks in.",
        json_schema_extra={
            "examples": GOOGLE_VERTEX_REALTIME_VOICES,
            "allow_custom_input": True,
        },
    )
    language: str = Field(
        default="en",
        description="BCP-47 language code (e.g. 'en-US').",
        json_schema_extra={
            "examples": GOOGLE_VERTEX_REALTIME_LANGUAGES,
            "allow_custom_input": True,
        },
    )
    project_id: str = Field(description="Google Cloud project ID for Vertex AI.")
    location: str = Field(
        default="global",
        description="GCP region for the Vertex AI endpoint (e.g. 'global').",
    )
    credentials: str | None = Field(
        default=None,
        description=(
            "Paste the entire service-account JSON file contents. If omitted, "
            "falls back to Application Default Credentials (ADC)."
        ),
        json_schema_extra={"multiline": True},
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description=(
            "Not used for Vertex AI — authentication is via the service account "
            "in `credentials` (or ADC). Leave blank."
        ),
    )


REALTIME_PROVIDERS = {
    ServiceProviders.OPENAI_REALTIME.value,
    ServiceProviders.GROK_REALTIME.value,
    ServiceProviders.ULTRAVOX_REALTIME.value,
    ServiceProviders.GOOGLE_REALTIME.value,
    ServiceProviders.GOOGLE_VERTEX_REALTIME.value,
}


LLMConfig = Annotated[
    Union[
        OpenAILLMService,
        AnthropicLLMConfiguration,
        GoogleVertexLLMConfiguration,
        GroqLLMService,
        XAILLMConfiguration,
        OpenRouterLLMConfiguration,
        GoogleLLMService,
        AzureLLMService,
        DograhLLMService,
        AWSBedrockLLMConfiguration,
        SpeachesLLMConfiguration,
        MiniMaxLLMConfiguration,
        MistralLLMConfiguration,
        TogetherLLMConfiguration,
        CerebrasLLMConfiguration,
        FireworksLLMConfiguration,
        CohereLLMConfiguration,
    ],
    Field(discriminator="provider"),
]

RealtimeConfig = Annotated[
    Union[
        OpenAIRealtimeLLMConfiguration,
        GrokRealtimeLLMConfiguration,
        UltravoxRealtimeLLMConfiguration,
        GoogleRealtimeLLMConfiguration,
        GoogleVertexRealtimeLLMConfiguration,
    ],
    Field(discriminator="provider"),
]

###################################################### TTS ########################################################################


@register_tts
class DeepgramTTSConfiguration(BaseServiceConfiguration):
    model_config = DEEPGRAM_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.DEEPGRAM] = ServiceProviders.DEEPGRAM
    voice: str = Field(
        default="aura-2-helena-en",
        description="Deepgram voice ID (model is inferred from the 'aura-N' prefix).",
    )

    @computed_field
    @property
    def model(self) -> str:
        # Deepgram model's name is inferred using the voice name.
        # It can either contain aura-2 or aura-1
        if "aura-2" in self.voice:
            return "aura-2"
        elif "aura-1" in self.voice:
            return "aura-1"
        else:
            # Default fallback
            return "aura-2"


ELEVENLABS_TTS_MODELS = ["eleven_flash_v2_5"]


@register_tts
class ElevenlabsTTSConfiguration(BaseServiceConfiguration):
    model_config = ELEVENLABS_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.ELEVENLABS] = ServiceProviders.ELEVENLABS
    voice: str = Field(
        default="21m00Tcm4TlvDq8ikWAM",
        description="ElevenLabs voice ID from your Voice Library.",
    )
    speed: float = Field(default=1.0, ge=0.1, le=2.0, description="Speed of the voice.")
    model: str = Field(
        default="eleven_flash_v2_5",
        description="ElevenLabs TTS model.",
        json_schema_extra={"examples": ELEVENLABS_TTS_MODELS},
    )
    base_url: str = Field(
        default="https://api.elevenlabs.io",
        description=(
            "ElevenLabs API base URL. Override to use a Data Residency endpoint "
            "(e.g. https://api.eu.residency.elevenlabs.io) for GDPR / HIPAA / "
            "regional compliance."
        ),
    )


@register_tts
class GoogleTTSConfiguration(BaseTTSConfiguration):
    model_config = GOOGLE_CLOUD_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GOOGLE] = ServiceProviders.GOOGLE
    model: str = Field(
        default="chirp_3_hd",
        description=(
            "Google Cloud low-latency TTS engine. Dograh maps this to Pipecat's "
            "streaming Google TTS service for Chirp 3 HD and Journey voices."
        ),
        json_schema_extra={
            "examples": GOOGLE_TTS_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="en-US-Chirp3-HD-Charon",
        description="Google Cloud voice name. Use a Chirp 3 HD or Journey voice for streaming TTS.",
        json_schema_extra={
            "examples": GOOGLE_TTS_VOICES,
            "allow_custom_input": True,
        },
    )
    language: str = Field(
        default="en-US",
        description="BCP-47 language code for synthesis.",
        json_schema_extra={
            "examples": GOOGLE_TTS_LANGUAGES,
            "allow_custom_input": True,
        },
    )
    speed: float = Field(
        default=1.0,
        ge=0.25,
        le=2.0,
        description="Speech speed multiplier for Google streaming TTS.",
    )
    location: str | None = Field(
        default=None,
        description=(
            "Optional Google Cloud regional Text-to-Speech endpoint (for example "
            "'us-central1'). Leave blank to use the default endpoint."
        ),
    )
    credentials: str | None = Field(
        default=None,
        description=(
            "Paste the entire Google Cloud service-account JSON. If omitted, "
            "the server falls back to Application Default Credentials (ADC)."
        ),
        json_schema_extra={"multiline": True},
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Not used for Google Cloud TTS. Leave blank.",
    )


OPENAI_TTS_MODELS = ["gpt-4o-mini-tts"]


@register_tts
class OpenAITTSService(BaseTTSConfiguration):
    model_config = OPENAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENAI] = ServiceProviders.OPENAI
    model: str = Field(
        default="gpt-4o-mini-tts",
        description="OpenAI TTS model.",
        json_schema_extra={"examples": OPENAI_TTS_MODELS},
    )
    voice: str = Field(
        default="alloy",
        description="OpenAI TTS voice name.",
    )


DOGRAH_TTS_MODELS = ["default"]


@register_tts
class DograhTTSService(BaseTTSConfiguration):
    model_config = DOGRAH_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.DOGRAH] = ServiceProviders.DOGRAH
    model: str = Field(
        default="default",
        description="Dograh TTS tier.",
        json_schema_extra={"examples": DOGRAH_TTS_MODELS},
    )
    voice: str = Field(
        default="default",
        description="Voice preset.",
    )
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="Speed of the voice.")


CARTESIA_TTS_MODELS = ["sonic-3"]


@register_tts
class CartesiaTTSConfiguration(BaseTTSConfiguration):
    model_config = CARTESIA_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.CARTESIA] = ServiceProviders.CARTESIA
    model: str = Field(
        default="sonic-3",
        description="Cartesia TTS model.",
        json_schema_extra={"examples": CARTESIA_TTS_MODELS},
    )
    voice: str = Field(
        default="3faa81ae-d3d8-4ab1-9e44-e50e46d33c30",
        description="Cartesia voice UUID from your Cartesia dashboard.",
    )
    speed: float = Field(default=1.0, ge=0.6, le=1.5, description="Speed of the voice.")
    volume: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="Volume multiplier for generated speech.",
    )


@register_tts
class SarvamTTSConfiguration(BaseTTSConfiguration):
    model_config = SARVAM_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.SARVAM] = ServiceProviders.SARVAM
    model: str = Field(
        default="bulbul:v2",
        description="Sarvam TTS model (voice list depends on this).",
        json_schema_extra={"examples": SARVAM_TTS_MODELS},
    )
    voice: str = Field(
        default="anushka",
        description="Sarvam voice name; must match the selected model's voice list.",
        json_schema_extra={
            "examples": SARVAM_V2_VOICES,
            "model_options": {
                "bulbul:v2": SARVAM_V2_VOICES,
                "bulbul:v3": SARVAM_V3_VOICES,
            },
        },
    )
    language: str = Field(
        default="hi-IN",
        description="BCP-47 Indian-language code (e.g. hi-IN, en-IN).",
        json_schema_extra={"examples": SARVAM_LANGUAGES},
    )


CAMB_TTS_MODELS = ["mars-flash", "mars-pro", "mars-instruct"]


@register_tts
class CambTTSConfiguration(BaseTTSConfiguration):
    model_config = CAMB_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.CAMB] = ServiceProviders.CAMB
    model: str = Field(
        default="mars-flash",
        description="Camb.ai TTS model.",
        json_schema_extra={"examples": CAMB_TTS_MODELS},
    )
    voice: str = Field(default="147320", description="Camb.ai voice ID.")
    language: str = Field(default="en-us", description="BCP-47 language code.")


RIME_TTS_MODELS = ["arcana", "mistv3", "mistv2", "mist"]
RIME_TTS_LANGUAGES = ["en", "de", "fr", "es", "hi"]


@register_tts
class RimeTTSConfiguration(BaseTTSConfiguration):
    model_config = RIME_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.RIME] = ServiceProviders.RIME
    model: str = Field(
        default="arcana",
        description="Rime TTS model.",
        json_schema_extra={"examples": RIME_TTS_MODELS, "allow_custom_input": True},
    )
    voice: str = Field(
        default="celeste",
        description="Rime voice ID.",
    )
    speed: float = Field(
        default=1.0, ge=0.5, le=2.0, description="Speech speed multiplier."
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
        json_schema_extra={"examples": RIME_TTS_LANGUAGES, "allow_custom_input": True},
    )


SPEACHES_TTS_MODELS = ["hexgrad/Kokoro-82M"]


@register_tts
class SpeachesTTSConfiguration(BaseTTSConfiguration):
    model_config = SPEACHES_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.SPEACHES] = ServiceProviders.SPEACHES
    model: str = Field(
        default="kokoro",
        description="Model name as served by your TTS endpoint (e.g. Kokoro-FastAPI).",
        json_schema_extra={
            "examples": SPEACHES_TTS_MODELS,
            "allow_custom_input": True,
        },
    )
    voice: str = Field(
        default="af_heart",
        json_schema_extra={"allow_custom_input": True},
        description="Voice ID for the TTS engine.",
    )
    base_url: str = Field(
        default="http://localhost:8000/v1",
        description="OpenAI-compatible TTS endpoint (Kokoro-FastAPI, etc.).",
    )
    speed: float = Field(
        default=1.0, ge=0.25, le=4.0, description="Speech speed (0.25 to 4.0)."
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Usually not required for self-hosted TTS. Leave blank unless enforced.",
    )


MINIMAX_TTS_MODELS = ["speech-2.8-hd", "speech-2.8-turbo"]
MINIMAX_TTS_VOICES = [
    "English_Graceful_Lady",
    "English_Insightful_Speaker",
    "English_radiant_girl",
    "English_Persuasive_Man",
    "English_Lucky_Robot",
    "English_expressive_narrator",
]


@register_tts
class MiniMaxTTSConfiguration(BaseTTSConfiguration):
    provider: Literal[ServiceProviders.MINIMAX] = ServiceProviders.MINIMAX
    model: str = Field(
        default="speech-2.8-hd",
        description="MiniMax TTS model.",
        json_schema_extra={"examples": MINIMAX_TTS_MODELS},
    )
    voice: str = Field(
        default="English_Graceful_Lady",
        description="MiniMax voice ID.",
        json_schema_extra={"examples": MINIMAX_TTS_VOICES, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.minimax.io/v1/t2a_v2",
        description=(
            "MiniMax TTS API endpoint (must include the /v1/t2a_v2 path). "
            "Defaults to the global endpoint; override with "
            "https://api.minimaxi.chat/v1/t2a_v2 (mainland China) or "
            "https://api-uw.minimax.io/v1/t2a_v2 (US-West)."
        ),
    )
    speed: float = Field(
        default=1.0, ge=0.5, le=2.0, description="Speech speed (0.5 to 2.0)."
    )
    group_id: str = Field(
        description="MiniMax Group ID (found in your MiniMax dashboard under Account → Group).",
    )


XAI_TTS_VOICES = ["eve", "ara", "rex", "sal", "leo"]


@register_tts
class XAITTSConfiguration(BaseTTSConfiguration):
    model_config = XAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.XAI] = ServiceProviders.XAI
    model: str = Field(
        default="grok-voice-latest",
        description="xAI TTS model.",
        json_schema_extra={"examples": ["grok-voice-latest"]},
    )
    voice: str = Field(
        default="eve",
        description="xAI voice ID.",
        json_schema_extra={"examples": XAI_TTS_VOICES},
    )


AWS_POLLY_ENGINES = ["neural", "long-form", "standard", "generative"]
AWS_POLLY_VOICES = [
    "Joanna", "Matthew", "Ivy", "Kendra", "Kimberly", "Salli", "Joey", "Justin",
    "Kevin", "Ruth", "Stephen", "Amy", "Brian", "Emma", "Aria",
    "Ayanda", "Camila", "Chantal", "Conchita", "Daniel", "Enrique", "Filiz",
    "Gabrielle", "Geraint", "Giorgio", "Gwyneth", "Hans", "Ines", "Karl",
    "Lotte", "Lucia", "Lupe", "Mads", "Maja", "Marlene", "Mathieu",
    "Miguel", "Mizuki", "Naja", "Nicole", "Pedro", "Penelope", "Raveena",
    "Ruben", "Russell", "Seoyeon", "Takumi", "Tatyana", "Vicki", "Vitoria",
    "Zeina", "Zhiyu",
]


@register_tts
class AWSPollyTTSConfiguration(BaseTTSConfiguration):
    model_config = AWS_POLLY_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.AWS_POLLY] = ServiceProviders.AWS_POLLY
    model: str = Field(
        default="neural",
        description="AWS Polly engine type. 'neural' for high-quality voices, 'standard' for legacy, 'long-form' for long content, 'generative' for generative voices.",
        json_schema_extra={"examples": AWS_POLLY_ENGINES},
    )
    voice: str = Field(
        default="Joanna",
        description="AWS Polly voice ID (e.g. Joanna, Matthew, Amy).",
        json_schema_extra={"examples": AWS_POLLY_VOICES, "allow_custom_input": True},
    )
    aws_access_key: str = Field(
        default="",
        description="AWS access key ID with polly:SynthesizeSpeech permission.",
    )
    aws_secret_key: str = Field(
        default="",
        description="AWS secret access key paired with the access key ID.",
    )
    aws_region: str = Field(
        default="us-east-1",
        description="AWS region where Amazon Polly is available.",
    )
    language: str = Field(
        default="en-US",
        description="BCP-47 language code for synthesis.",
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Not used for AWS Polly — authentication is via AWS credentials above. Leave blank.",
    )


AZURE_TTS_VOICES = [
    "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural", "en-US-DavisNeural",
    "en-US-AmberNeural", "en-US-AnaNeural", "en-US-AshleyNeural", "en-US-BrandonNeural",
    "en-US-ChristopherNeural", "en-US-CoraNeural", "en-US-ElizabethNeural",
    "en-US-EricNeural", "en-US-JacobNeural", "en-US-JaneNeural",
    "en-GB-SoniaNeural", "en-GB-RyanNeural", "en-GB-LibbyNeural", "en-GB-MaisieNeural",
    "en-AU-NatashaNeural", "en-AU-WilliamNeural",
    "en-IN-NeerjaNeural", "en-IN-PrabhatNeural",
    "es-ES-ElviraNeural", "es-ES-AlvaroNeural",
    "fr-FR-DeniseNeural", "fr-FR-HenriNeural",
    "de-DE-KatjaNeural", "de-DE-ConradNeural",
    "it-IT-ElsaNeural", "it-IT-DiegoNeural",
    "pt-BR-FranciscaNeural", "pt-BR-AntonioNeural",
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
    "ja-JP-NanamiNeural", "ja-JP-KeitaNeural",
    "ko-KR-SunHiNeural", "ko-KR-InJoonNeural",
    "ar-SA-ZariyahNeural", "ar-SA-HamedNeural",
    "hi-IN-SwaraNeural", "hi-IN-MadhurNeural",
]


@register_tts
class AzureTTSConfiguration(BaseTTSConfiguration):
    model_config = AZURE_TTS_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.AZURE_TTS] = ServiceProviders.AZURE_TTS
    model: str = Field(
        default="neural",
        description="Azure TTS engine type (neural, standard). Neural recommended for quality.",
        json_schema_extra={"examples": ["neural", "standard"]},
    )
    voice: str = Field(
        default="en-US-JennyNeural",
        description="Azure voice name (e.g. en-US-JennyNeural).",
        json_schema_extra={"examples": AZURE_TTS_VOICES, "allow_custom_input": True},
    )
    region: str = Field(
        default="eastus",
        description="Azure region for the Speech service (e.g. eastus, westus2, eastasia).",
    )
    language: str = Field(
        default="en-US",
        description="BCP-47 language code for synthesis.",
    )
    speed: float = Field(
        default=1.0, ge=0.5, le=2.0, description="Speech speed multiplier."
    )


PLAYHT_TTS_MODELS = ["Play3.0-mini", "PlayHT2.0-turbo", "PlayHT2.0", "PlayDialog"]
PLAYHT_TTS_VOICES = [
    "s3://voice-cloning-zero-shot/d9ff78ba-d016-47f6-b0ef-dd630f59414e/female-cs/manifest.json",
    "s3://voice-cloning-zero-shot/baf1ef41-36b6-428c-9bdf-50ba54682bd8/original/manifest.json",
    "s3://peregrine-voices/mel parrot/manifest.json",
    "s3://peregrine-voices/oliver/manifest.json",
    "s3://peregrine-voices/ryan/manifest.json",
]


@register_tts
class PlayHTTTSConfiguration(BaseTTSConfiguration):
    model_config = PLAYHT_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.PLAYHT] = ServiceProviders.PLAYHT
    model: str = Field(
        default="Play3.0-mini",
        description="PlayHT TTS model.",
        json_schema_extra={"examples": PLAYHT_TTS_MODELS},
    )
    voice: str = Field(
        default="s3://voice-cloning-zero-shot/d9ff78ba-d016-47f6-b0ef-dd630f59414e/female-cs/manifest.json",
        description="PlayHT voice S3 manifest URL or cloned voice ID.",
        json_schema_extra={"examples": PLAYHT_TTS_VOICES, "allow_custom_input": True},
    )
    user_id: str = Field(
        description="PlayHT User ID from your PlayHT dashboard.",
    )
    language: str = Field(
        default="english",
        description="Language name for synthesis (e.g. 'english', 'spanish').",
    )
    speed: float = Field(
        default=1.0, ge=0.1, le=5.0, description="Speech speed multiplier."
    )


NEETS_TTS_MODELS = ["style-diff-500", "ar-diff-50k", "vits"]
NEETS_TTS_VOICES = [
    "us-female-2", "us-male-2", "us-female-5", "us-male-5",
    "clara", "james", "aria", "ryan", "sophia", "oliver",
]


@register_tts
class NeetsTTSConfiguration(BaseTTSConfiguration):
    model_config = NEETS_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.NEETS] = ServiceProviders.NEETS
    model: str = Field(
        default="style-diff-500",
        description="Neets.ai TTS model.",
        json_schema_extra={"examples": NEETS_TTS_MODELS},
    )
    voice: str = Field(
        default="us-female-2",
        description="Neets.ai voice ID.",
        json_schema_extra={"examples": NEETS_TTS_VOICES, "allow_custom_input": True},
    )
    base_url: str = Field(
        default="https://api.neets.ai/v1/tts",
        description="Neets.ai API endpoint.",
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
    )


TTSConfig = Annotated[
    Union[
        DeepgramTTSConfiguration,
        GoogleTTSConfiguration,
        OpenAITTSService,
        ElevenlabsTTSConfiguration,
        CartesiaTTSConfiguration,
        DograhTTSService,
        SarvamTTSConfiguration,
        CambTTSConfiguration,
        RimeTTSConfiguration,
        SpeachesTTSConfiguration,
        MiniMaxTTSConfiguration,
        XAITTSConfiguration,
        AWSPollyTTSConfiguration,
        AzureTTSConfiguration,
        PlayHTTTSConfiguration,
        NeetsTTSConfiguration,
    ],
    Field(discriminator="provider"),
]

###################################################### STT ########################################################################


@register_stt
class DeepgramSTTConfiguration(BaseSTTConfiguration):
    model_config = DEEPGRAM_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.DEEPGRAM] = ServiceProviders.DEEPGRAM
    model: str = Field(
        default="nova-3-general",
        description="Deepgram STT model.",
        json_schema_extra={"examples": DEEPGRAM_STT_MODELS},
    )
    language: str = Field(
        default="multi",
        description="Language code; 'multi' enables auto-detect (Nova-3 only).",
        json_schema_extra={
            "examples": DEEPGRAM_LANGUAGES,
            "model_options": {
                "nova-3-general": DEEPGRAM_LANGUAGES,
                "flux-general-en": ("en",),
            },
        },
    )


CARTESIA_STT_MODELS = ["ink-whisper"]


@register_stt
class CartesiaSTTConfiguration(BaseSTTConfiguration):
    model_config = CARTESIA_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.CARTESIA] = ServiceProviders.CARTESIA
    model: str = Field(
        default="ink-whisper",
        description="Cartesia STT model.",
        json_schema_extra={"examples": CARTESIA_STT_MODELS},
    )


OPENAI_STT_MODELS = ["gpt-4o-transcribe"]


@register_stt
class OpenAISTTConfiguration(BaseSTTConfiguration):
    model_config = OPENAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENAI] = ServiceProviders.OPENAI
    model: str = Field(
        default="gpt-4o-transcribe",
        description="OpenAI transcription model.",
        json_schema_extra={"examples": OPENAI_STT_MODELS},
    )


@register_stt
class GoogleSTTConfiguration(BaseSTTConfiguration):
    model_config = GOOGLE_CLOUD_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GOOGLE] = ServiceProviders.GOOGLE
    model: str = Field(
        default="latest_long",
        description="Google Cloud Speech-to-Text V2 recognition model.",
        json_schema_extra={
            "examples": GOOGLE_STT_MODELS,
            "allow_custom_input": True,
        },
    )
    language: str = Field(
        default="en-US",
        description="Primary BCP-47 language code for recognition.",
        json_schema_extra={
            "examples": GOOGLE_STT_LANGUAGES,
            "allow_custom_input": True,
            "docs_url": "https://docs.cloud.google.com/speech-to-text/docs/speech-to-text-supported-languages",
        },
    )
    location: str = Field(
        default="global",
        description="Google Cloud Speech-to-Text region (for example 'global' or 'us-central1').",
    )
    credentials: str | None = Field(
        default=None,
        description=(
            "Paste the entire Google Cloud service-account JSON. If omitted, "
            "the server falls back to Application Default Credentials (ADC)."
        ),
        json_schema_extra={"multiline": True},
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Not used for Google Cloud STT. Leave blank.",
    )


# Dograh STT Service
DOGRAH_STT_MODELS = ["default"]
DOGRAH_STT_LANGUAGES = DEEPGRAM_LANGUAGES


@register_stt
class DograhSTTService(BaseSTTConfiguration):
    model_config = DOGRAH_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.DOGRAH] = ServiceProviders.DOGRAH
    model: str = Field(
        default="default",
        description="Dograh STT tier.",
        json_schema_extra={"examples": DOGRAH_STT_MODELS},
    )
    language: str = Field(
        default="multi",
        description="Language code; use 'multi' for auto-detect.",
        json_schema_extra={"examples": DOGRAH_STT_LANGUAGES},
    )


@register_stt
class SarvamSTTConfiguration(BaseSTTConfiguration):
    model_config = SARVAM_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.SARVAM] = ServiceProviders.SARVAM
    model: str = Field(
        default="saarika:v2.5",
        description="Sarvam STT model.",
        json_schema_extra={"examples": SARVAM_STT_MODELS},
    )
    language: str = Field(
        default="hi-IN",
        description="BCP-47 Indian-language code.",
        json_schema_extra={"examples": SARVAM_LANGUAGES},
    )


@register_stt
class SpeechmaticsSTTConfiguration(BaseSTTConfiguration):
    model_config = SPEECHMATICS_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.SPEECHMATICS] = ServiceProviders.SPEECHMATICS
    model: str = Field(
        default="enhanced",
        description="Speechmatics operating point: 'standard' or 'enhanced'.",
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
        json_schema_extra={"examples": SPEECHMATICS_STT_LANGUAGES},
    )


SPEACHES_STT_MODELS = [
    "Systran/faster-distil-whisper-small.en",
    "Systran/faster-whisper-large-v3",
]
SPEACHES_STT_LANGUAGES = ["en", "ar", "nl", "fr", "de", "hi", "it", "pt", "es"]


@register_stt
class SpeachesSTTConfiguration(BaseSTTConfiguration):
    model_config = SPEACHES_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.SPEACHES] = ServiceProviders.SPEACHES
    model: str = Field(
        default="Systran/faster-distil-whisper-small.en",
        description="Whisper model identifier as served by your STT endpoint.",
        json_schema_extra={
            "examples": SPEACHES_STT_MODELS,
            "allow_custom_input": True,
        },
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
        json_schema_extra={
            "examples": SPEACHES_STT_LANGUAGES,
            "allow_custom_input": True,
        },
    )
    base_url: str = Field(
        default="http://localhost:8000/v1",
        description="OpenAI-compatible STT endpoint (Speaches, etc.).",
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Usually not required for self-hosted STT. Leave blank unless enforced.",
    )


ASSEMBLYAI_STT_MODELS = ["u3-rt-pro"]
ASSEMBLYAI_STT_LANGUAGES = ["en", "es", "de", "fr", "pt", "it"]


@register_stt
class AssemblyAISTTConfiguration(BaseSTTConfiguration):
    model_config = ASSEMBLYAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.ASSEMBLYAI] = ServiceProviders.ASSEMBLYAI
    model: str = Field(
        default="u3-rt-pro",
        description="AssemblyAI realtime STT model.",
        json_schema_extra={"examples": ASSEMBLYAI_STT_MODELS},
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
        json_schema_extra={"examples": ASSEMBLYAI_STT_LANGUAGES},
    )


@register_stt
class GladiaSTTConfiguration(BaseSTTConfiguration):
    model_config = GLADIA_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.GLADIA] = ServiceProviders.GLADIA
    model: str = Field(
        default="solaria-1",
        description="Gladia STT model.",
        json_schema_extra={"examples": GLADIA_STT_MODELS},
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code.",
        json_schema_extra={"examples": GLADIA_STT_LANGUAGES},
    )


XAI_STT_MODELS = ["xai-speech-v1"]

XAI_STT_LANGUAGES = [
    "ar", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fr", "hi", "hu", "id",
    "it", "ja", "ko", "mk", "ms", "nl", "pl", "pt", "ro", "ru", "sv", "th", "tr", "vi",
]


@register_stt
class XAISTTConfiguration(BaseSTTConfiguration):
    model_config = XAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.XAI] = ServiceProviders.XAI
    model: str = Field(
        default="xai-speech-v1",
        description="xAI speech-to-text service.",
        json_schema_extra={"examples": XAI_STT_MODELS},
    )
    language: str = Field(
        default="en",
        description="BCP-47 language code for transcription (e.g. 'en', 'es', 'fr').",
        json_schema_extra={"examples": XAI_STT_LANGUAGES},
    )


AZURE_SPEECH_STT_MODELS = ["realtime", "whisper"]
AZURE_SPEECH_STT_LANGUAGES = [
    "en-US", "en-GB", "en-AU", "en-IN", "es-ES", "es-MX",
    "fr-FR", "de-DE", "it-IT", "pt-BR", "zh-CN", "ja-JP",
    "ko-KR", "ar-SA", "hi-IN", "nl-NL", "pl-PL", "ru-RU",
    "sv-SE", "tr-TR", "vi-VN", "th-TH",
]


@register_stt
class AzureSpeechSTTConfiguration(BaseSTTConfiguration):
    model_config = AZURE_SPEECH_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.AZURE_SPEECH] = ServiceProviders.AZURE_SPEECH
    model: str = Field(
        default="realtime",
        description="Azure Speech model type. 'realtime' for live transcription, 'whisper' for Whisper on Azure.",
        json_schema_extra={"examples": AZURE_SPEECH_STT_MODELS},
    )
    region: str = Field(
        default="eastus",
        description="Azure region for the Speech service (e.g. eastus, westus2).",
    )
    language: str = Field(
        default="en-US",
        description="Primary BCP-47 language code for recognition.",
        json_schema_extra={"examples": AZURE_SPEECH_STT_LANGUAGES, "allow_custom_input": True},
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Azure Cognitive Services subscription key.",
    )


AWS_TRANSCRIBE_STT_MODELS = ["general", "phone-call", "dictation"]
AWS_TRANSCRIBE_STT_LANGUAGES = [
    "en-US", "en-GB", "en-AU", "en-IN", "es-US", "es-ES",
    "fr-FR", "fr-CA", "de-DE", "it-IT", "pt-BR", "zh-CN",
    "ja-JP", "ko-KR", "ar-SA", "hi-IN", "nl-NL", "ru-RU",
]


@register_stt
class AWSTranscribeSTTConfiguration(BaseSTTConfiguration):
    model_config = AWS_TRANSCRIBE_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.AWS_TRANSCRIBE] = ServiceProviders.AWS_TRANSCRIBE
    model: str = Field(
        default="general",
        description="Transcribe specialty model: 'general' for broad use, 'phone-call' for telephony, 'dictation' for dictation.",
        json_schema_extra={"examples": AWS_TRANSCRIBE_STT_MODELS},
    )
    language: str = Field(
        default="en-US",
        description="BCP-47 language code for transcription.",
        json_schema_extra={"examples": AWS_TRANSCRIBE_STT_LANGUAGES, "allow_custom_input": True},
    )
    aws_access_key: str = Field(
        default="",
        description="AWS access key ID with transcribe:StartStreamTranscription permission.",
    )
    aws_secret_key: str = Field(
        default="",
        description="AWS secret access key paired with the access key ID.",
    )
    aws_region: str = Field(
        default="us-east-1",
        description="AWS region where Amazon Transcribe is available.",
    )
    api_key: str | list[str] | None = Field(
        default=None,
        description="Not used for AWS Transcribe — authentication is via AWS credentials above. Leave blank.",
    )


STTConfig = Annotated[
    Union[
        DeepgramSTTConfiguration,
        CartesiaSTTConfiguration,
        OpenAISTTConfiguration,
        GoogleSTTConfiguration,
        DograhSTTService,
        SpeechmaticsSTTConfiguration,
        SarvamSTTConfiguration,
        SpeachesSTTConfiguration,
        AssemblyAISTTConfiguration,
        GladiaSTTConfiguration,
        XAISTTConfiguration,
        AzureSpeechSTTConfiguration,
        AWSTranscribeSTTConfiguration,
    ],
    Field(discriminator="provider"),
]

###################################################### EMBEDDINGS ########################################################################

OPENAI_EMBEDDING_MODELS = ["text-embedding-3-small"]


@register_embeddings
class OpenAIEmbeddingsConfiguration(BaseEmbeddingsConfiguration):
    model_config = OPENAI_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENAI] = ServiceProviders.OPENAI
    model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model.",
        json_schema_extra={"examples": OPENAI_EMBEDDING_MODELS},
    )


OPENROUTER_EMBEDDING_MODELS = ["openai/text-embedding-3-small"]


@register_embeddings
class OpenRouterEmbeddingsConfiguration(BaseEmbeddingsConfiguration):
    model_config = OPENROUTER_PROVIDER_MODEL_CONFIG
    provider: Literal[ServiceProviders.OPENROUTER] = ServiceProviders.OPENROUTER
    model: str = Field(
        default="openai/text-embedding-3-small",
        description="OpenRouter-hosted embedding model slug.",
        json_schema_extra={"examples": OPENROUTER_EMBEDDING_MODELS},
    )

    base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="Override only if proxying OpenRouter through your own gateway.",
    )


EmbeddingsConfig = Annotated[
    Union[OpenAIEmbeddingsConfiguration, OpenRouterEmbeddingsConfiguration],
    Field(discriminator="provider"),
]

ServiceConfig = Annotated[
    Union[LLMConfig, RealtimeConfig, TTSConfig, STTConfig, EmbeddingsConfig],
    Field(discriminator="provider"),
]
