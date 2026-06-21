import re
from typing import TYPE_CHECKING

import aiohttp
from fastapi import HTTPException
from loguru import logger

from api.constants import MPS_API_URL
from api.services.configuration.masking import contains_masked_key
from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.elevenlabs_tts import DograhElevenLabsTTSService
from api.services.pipecat.minimax_llm import DograhMiniMaxLLMService
from api.services.pipecat.minimax_tts import MiniMaxOwnedSessionTTSService
from api.utils.url_security import validate_user_configured_service_url
from pipecat.services.assemblyai.stt import AssemblyAISTTService, AssemblyAISTTSettings
from pipecat.services.aws.llm import AWSBedrockLLMService, AWSBedrockLLMSettings
from pipecat.services.aws.stt import AWSTranscribeSTTService, AWSTranscribeSTTSettings
from pipecat.services.aws.tts import AWSPollyTTSService, AWSPollyTTSSettings
from pipecat.services.azure.llm import AzureLLMService, AzureLLMSettings
from pipecat.services.azure.stt import AzureSTTService, AzureSTTSettings
from pipecat.services.azure.tts import AzureTTSService, AzureTTSSettings
from pipecat.services.cerebras.llm import CerebrasLLMService, CerebrasLLMSettings
from pipecat.services.fireworks.llm import FireworksLLMService, FireworksLLMSettings
from pipecat.services.mistral.llm import MistralLLMService, MistralLLMSettings
from pipecat.services.together.llm import TogetherLLMService, TogetherLLMSettings
from pipecat.services.cartesia.stt import CartesiaSTTService
from pipecat.services.cartesia.tts import (
    CartesiaTTSService,
    CartesiaTTSSettings,
    GenerationConfig,
)
from pipecat.services.deepgram.flux.stt import (
    DeepgramFluxSTTService,
    DeepgramFluxSTTSettings,
)
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.deepgram.tts import DeepgramTTSService, DeepgramTTSSettings
from pipecat.services.dograh.llm import DograhLLMService
from pipecat.services.dograh.stt import DograhSTTService, DograhSTTSettings
from pipecat.services.dograh.tts import DograhTTSService, DograhTTSSettings
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService, ElevenLabsTTSSettings
from pipecat.services.gladia.stt import GladiaSTTService, GladiaSTTSettings
from pipecat.services.google.llm import GoogleLLMService, GoogleLLMSettings
from pipecat.services.google.stt import GoogleSTTService, GoogleSTTSettings
from pipecat.services.google.tts import GoogleHttpTTSService, GoogleHttpTTSSettings, GoogleTTSService, GoogleTTSSettings
from pipecat.services.google.vertex.llm import (
    GoogleVertexLLMService,
    GoogleVertexLLMSettings,
)
from pipecat.services.groq.llm import GroqLLMService, GroqLLMSettings
from pipecat.services.minimax.tts import MiniMaxTTSSettings
from pipecat.services.openai.base_llm import OpenAILLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import (
    OpenAISTTService,
    OpenAISTTSettings,
)
from pipecat.services.openai.tts import OpenAITTSService, OpenAITTSSettings
from pipecat.services.openrouter.llm import OpenRouterLLMService, OpenRouterLLMSettings
from pipecat.services.rime.tts import RimeTTSService, RimeTTSSettings
from pipecat.services.xai.stt import XAISTTService, XAISTTSettings
from pipecat.services.xai.tts import XAITTSService, XAIWebsocketTTSSettings
from pipecat.services.sarvam.stt import SarvamSTTService, SarvamSTTSettings
from pipecat.services.sarvam.tts import SarvamTTSService, SarvamTTSSettings
from pipecat.services.speaches.llm import SpeachesLLMService, SpeachesLLMSettings
from pipecat.services.speaches.stt import SpeachesSTTService, SpeachesSTTSettings
from pipecat.services.speaches.tts import SpeachesTTSService, SpeachesTTSSettings
from pipecat.services.speechmatics.stt import (
    SpeechmaticsSTTService,
    SpeechmaticsSTTSettings,
)
from pipecat.transcriptions.language import Language
from pipecat.utils.text.xml_function_tag_filter import XMLFunctionTagFilter

if TYPE_CHECKING:
    from api.services.pipecat.audio_config import AudioConfig


def _validate_runtime_service_url(url: str, field_name: str) -> None:
    try:
        validate_user_configured_service_url(
            url,
            field_name=field_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def create_stt_service(
    user_config, audio_config: "AudioConfig", keyterms: list[str] | None = None
):
    """Create and return appropriate STT service based on user configuration

    Args:
        user_config: User configuration containing STT settings
        keyterms: Optional list of keyterms for speech recognition boosting (Deepgram only)
    """
    logger.info(
        f"Creating STT service: provider={user_config.stt.provider}, model={user_config.stt.model}"
    )
    if user_config.stt.provider == ServiceProviders.DEEPGRAM.value:
        logger.info(
            f"Deepgram STT: model={user_config.stt.model!r} api_key_set={bool(user_config.stt.api_key)}"
        )
        # Check if using Flux model (English-only, no language selection)
        if user_config.stt.model == "flux-general-en":
            return DeepgramFluxSTTService(
                api_key=user_config.stt.api_key,
                settings=DeepgramFluxSTTSettings(
                    model=user_config.stt.model,
                    eot_timeout_ms=3000,
                    eot_threshold=0.7,
                    eager_eot_threshold=0.5,
                    keyterm=keyterms or [],
                ),
                should_interrupt=False,  # Let UserAggregator take care of sending InterruptionFrame
                sample_rate=audio_config.transport_in_sample_rate,
            )

        # Other models than flux
        # Use language from user config, defaulting to "multi" for multilingual support
        language = getattr(user_config.stt, "language", None) or "multi"
        return DeepgramSTTService(
            api_key=user_config.stt.api_key,
            settings=DeepgramSTTSettings(
                language=language,
                profanity_filter=False,
                endpointing=100,
                model=user_config.stt.model,
                keyterm=keyterms or [],
            ),
            should_interrupt=False,  # Let UserAggregator take care of sending InterruptionFrame
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.OPENAI.value:
        return OpenAISTTService(
            api_key=user_config.stt.api_key,
            settings=OpenAISTTSettings(model=user_config.stt.model),
        )
    elif user_config.stt.provider == ServiceProviders.GOOGLE.value:
        language = getattr(user_config.stt, "language", None) or "en-US"
        location = getattr(user_config.stt, "location", None) or "global"
        # Chirp models (chirp, chirp_2, chirp_3, chirp-*) are region-specific and
        # do not exist in the "global" location. Fall back to us-central1.
        stt_model_name = (user_config.stt.model or "").lower()
        if location == "global" and stt_model_name.startswith("chirp"):
            location = "us-central1"
            logger.info(
                f"Google STT: chirp model '{user_config.stt.model}' is not available in "
                f"'global' — redirecting to location='us-central1'"
            )
        credentials = getattr(user_config.stt, "credentials", None)
        if credentials and contains_masked_key(str(credentials)):
            credentials = None
        # Some deployments store the service-account JSON in api_key instead of credentials.
        # Only use this fallback when the value actually looks like JSON (starts with '{').
        if not credentials:
            api_key_fallback = getattr(user_config.stt, "api_key", None) or None
            if api_key_fallback and not contains_masked_key(str(api_key_fallback)):
                if str(api_key_fallback).strip().startswith("{"):
                    credentials = api_key_fallback
        logger.info(
            f"Google STT: model={user_config.stt.model!r} credentials_set={bool(credentials)} "
            f"credentials_first_char={(str(credentials)[0] if credentials else 'N/A')!r}"
        )
        if not credentials:
            raise ValueError(
                "Google STT requires a service-account JSON credentials string. "
                "Go to AI Models → Transcriber and paste your Google Cloud service-account JSON."
            )
        import json as _json
        try:
            _json.loads(credentials)
        except _json.JSONDecodeError as exc:
            logger.error(
                f"Google STT: credentials is not valid JSON "
                f"(first 100 chars: {str(credentials)[:100]!r}): {exc}"
            )
            raise ValueError(
                "Google STT credentials must be a valid Google Cloud service-account JSON string. "
                "Go to AI Models → Transcriber and paste the full JSON key file content."
            ) from exc

        settings_kwargs = {"model": user_config.stt.model}
        try:
            settings_kwargs["languages"] = [Language(language)]
        except ValueError:
            settings_kwargs["language_codes"] = [language]

        return GoogleSTTService(
            credentials=credentials,
            location=location,
            settings=GoogleSTTSettings(**settings_kwargs),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.CARTESIA.value:
        return CartesiaSTTService(
            api_key=user_config.stt.api_key,
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.DOGRAH.value:
        base_url = MPS_API_URL.replace("http://", "ws://").replace("https://", "wss://")
        language = getattr(user_config.stt, "language", None) or "multi"
        return DograhSTTService(
            base_url=base_url,
            api_key=user_config.stt.api_key,
            settings=DograhSTTSettings(
                model=user_config.stt.model,
                language=language,
            ),
            keyterms=keyterms,
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.SARVAM.value:
        # Map Sarvam language code to pipecat Language enum
        language_mapping = {
            "bn-IN": Language.BN_IN,
            "gu-IN": Language.GU_IN,
            "hi-IN": Language.HI_IN,
            "kn-IN": Language.KN_IN,
            "ml-IN": Language.ML_IN,
            "mr-IN": Language.MR_IN,
            "ta-IN": Language.TA_IN,
            "te-IN": Language.TE_IN,
            "pa-IN": Language.PA_IN,
            "od-IN": Language.OR_IN,
            "en-IN": Language.EN_IN,
            "as-IN": Language.AS_IN,
        }
        language = getattr(user_config.stt, "language", None)
        pipecat_language = language_mapping.get(language, Language.HI_IN)
        return SarvamSTTService(
            api_key=user_config.stt.api_key,
            settings=SarvamSTTSettings(
                model=user_config.stt.model,
                language=pipecat_language,
            ),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.SPEACHES.value:
        language = getattr(user_config.stt, "language", None)
        _validate_runtime_service_url(user_config.stt.base_url, "base_url")
        return SpeachesSTTService(
            base_url=user_config.stt.base_url,
            api_key=user_config.stt.api_key or "none",
            settings=SpeachesSTTSettings(
                model=user_config.stt.model,
                language=language,
            ),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.ASSEMBLYAI.value:
        language = getattr(user_config.stt, "language", None)
        settings_kwargs = {"model": user_config.stt.model, "language": language}
        if keyterms:
            settings_kwargs["keyterms_prompt"] = keyterms
        return AssemblyAISTTService(
            api_key=user_config.stt.api_key,
            settings=AssemblyAISTTSettings(**settings_kwargs),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.GLADIA.value:
        from pipecat.services.gladia.config import LanguageConfig

        language = getattr(user_config.stt, "language", None) or "en"
        settings_kwargs = {
            "model": user_config.stt.model,
            "language_config": LanguageConfig(
                languages=[language], code_switching=False
            ),
        }
        return GladiaSTTService(
            api_key=user_config.stt.api_key,
            settings=GladiaSTTSettings(**settings_kwargs),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.SPEECHMATICS.value:
        from pipecat.services.speechmatics.stt import (
            AdditionalVocabEntry,
            OperatingPoint,
        )

        language = getattr(user_config.stt, "language", None) or "en"
        # Map model field to operating point (standard or enhanced)
        operating_point = (
            OperatingPoint.ENHANCED
            if user_config.stt.model == "enhanced"
            else OperatingPoint.STANDARD
        )
        # Convert keyterms to AdditionalVocabEntry objects for Speechmatics
        additional_vocab = []
        if keyterms:
            additional_vocab = [AdditionalVocabEntry(content=term) for term in keyterms]
        return SpeechmaticsSTTService(
            api_key=user_config.stt.api_key,
            settings=SpeechmaticsSTTSettings(
                language=language,
                operating_point=operating_point,
                additional_vocab=additional_vocab,
            ),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.XAI.value:
        language = getattr(user_config.stt, "language", None) or "en"
        try:
            lang_enum = Language(language)
        except ValueError:
            lang_enum = Language.EN
        return XAISTTService(
            api_key=user_config.stt.api_key,
            settings=XAISTTSettings(
                language=lang_enum,
                interim_results=True,
                endpointing=100,
            ),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.AZURE_SPEECH.value:
        api_key = user_config.stt.api_key
        region = getattr(user_config.stt, "region", None) or "eastus"
        language = getattr(user_config.stt, "language", None) or "en-US"
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail="Azure Speech STT requires an API key (Azure Cognitive Services subscription key).",
            )
        try:
            lang_enum = Language(language)
        except ValueError:
            lang_enum = Language.EN_US
        return AzureSTTService(
            api_key=api_key,
            region=region,
            settings=AzureSTTSettings(language=lang_enum),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    elif user_config.stt.provider == ServiceProviders.AWS_TRANSCRIBE.value:
        aws_access_key = getattr(user_config.stt, "aws_access_key", None) or None
        aws_secret_key = getattr(user_config.stt, "aws_secret_key", None) or None
        aws_region = getattr(user_config.stt, "aws_region", None) or "us-east-1"
        language = getattr(user_config.stt, "language", None) or "en-US"
        if not aws_access_key or not aws_secret_key:
            raise HTTPException(
                status_code=400,
                detail="AWS Transcribe requires aws_access_key and aws_secret_key. Configure them in your STT settings.",
            )
        try:
            lang_enum = Language(language)
        except ValueError:
            lang_enum = Language.EN_US
        return AWSTranscribeSTTService(
            aws_access_key_id=aws_access_key,
            api_key=aws_secret_key,
            region=aws_region,
            settings=AWSTranscribeSTTSettings(language=lang_enum),
            sample_rate=audio_config.transport_in_sample_rate,
        )
    else:
        raise HTTPException(
            status_code=400, detail=f"Invalid STT provider {user_config.stt.provider}"
        )


def create_tts_service(user_config, audio_config: "AudioConfig"):
    """Create and return appropriate TTS service based on user configuration

    Args:
        user_config: User configuration containing TTS settings
        transport_type: Type of transport (e.g., 'twilio', 'webrtc')
    """
    logger.info(
        f"Creating TTS service: provider={user_config.tts.provider}, model={user_config.tts.model}"
    )
    # Create function call filter to prevent TTS from speaking function call tags
    xml_function_tag_filter = XMLFunctionTagFilter()
    if user_config.tts.provider == ServiceProviders.DEEPGRAM.value:
        logger.info(
            f"Deepgram TTS: voice={user_config.tts.voice!r} api_key_set={bool(user_config.tts.api_key)}"
        )
        return DeepgramTTSService(
            api_key=user_config.tts.api_key,
            settings=DeepgramTTSSettings(voice=user_config.tts.voice),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.OPENAI.value:
        return OpenAITTSService(
            api_key=user_config.tts.api_key,
            # OpenAI TTS natively outputs 24kHz. Without this, the TTS service
            # inherits the pipeline's 16kHz rate from StartFrame and mislabels
            # 24kHz audio as 16kHz — causing the transport resampler to skip
            # resampling and play audio at 0.667x speed (slow motion effect).
            sample_rate=24000,
            settings=OpenAITTSSettings(
                model=user_config.tts.model,
                voice=user_config.tts.voice,
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.GOOGLE.value:
        model = getattr(user_config.tts, "model", None) or "chirp_3_hd"
        language = getattr(user_config.tts, "language", None) or "en-US"
        voice = getattr(user_config.tts, "voice", None) or "en-US-Chirp3-HD-Charon"
        speed = getattr(user_config.tts, "speed", None)
        location = getattr(user_config.tts, "location", None) or None
        credentials = getattr(user_config.tts, "credentials", None)
        if credentials and contains_masked_key(str(credentials)):
            credentials = None
        # Only treat api_key as credentials if it looks like JSON (starts with '{').
        # A regular Google API key (AIzaSy...) must not be passed to json.loads().
        if not credentials:
            api_key_fallback = getattr(user_config.tts, "api_key", None) or None
            if api_key_fallback and not contains_masked_key(str(api_key_fallback)):
                if str(api_key_fallback).strip().startswith("{"):
                    credentials = api_key_fallback
        logger.info(
            f"Google TTS: model={model!r} credentials_set={bool(credentials)} "
            f"credentials_first_char={(str(credentials)[0] if credentials else 'N/A')!r}"
        )
        if not credentials:
            raise ValueError(
                "Google TTS requires a service-account JSON credentials string. "
                "Go to AI Models → Voice Engine and paste your Google Cloud service-account JSON."
            )
        import json as _json
        try:
            _json.loads(credentials)
        except _json.JSONDecodeError as exc:
            logger.error(
                f"Google TTS: credentials is not valid JSON "
                f"(first 100 chars: {str(credentials)[:100]!r}): {exc}"
            )
            raise ValueError(
                "Google TTS credentials must be a valid Google Cloud service-account JSON string. "
                "Go to AI Models → Voice Engine and paste the full JSON key file content."
            ) from exc

        # Chirp 3 HD voices use the streaming API; all other voice families
        # (Wavenet, Neural2, Standard, Journey) require the HTTP batch API.
        is_chirp3_hd = "Chirp3-HD" in voice or "Chirp3_HD" in voice
        logger.info(
            f"Google TTS: voice={voice!r} is_chirp3_hd={is_chirp3_hd} "
            f"→ {'streaming' if is_chirp3_hd else 'http'} service"
        )

        if is_chirp3_hd:
            settings_kwargs = {
                "model": model,
                "voice": voice,
                "language": language,
            }
            if speed is not None and speed != 1.0:
                settings_kwargs["speaking_rate"] = speed
            return GoogleTTSService(
                credentials=credentials,
                location=location,
                settings=GoogleTTSSettings(**settings_kwargs),
                text_filters=[xml_function_tag_filter],
                skip_aggregator_types=["recording_router", "recording"],
                silence_time_s=1.0,
            )
        else:
            http_settings_kwargs = {
                "voice": voice,
                "language": language,
            }
            if speed is not None and speed != 1.0:
                http_settings_kwargs["speaking_rate"] = speed
            return GoogleHttpTTSService(
                credentials=credentials,
                location=location,
                settings=GoogleHttpTTSSettings(**http_settings_kwargs),
                text_filters=[xml_function_tag_filter],
                skip_aggregator_types=["recording_router", "recording"],
                silence_time_s=1.0,
            )
    elif user_config.tts.provider == ServiceProviders.ELEVENLABS.value:
        api_key = user_config.tts.api_key
        if api_key and contains_masked_key(str(api_key)):
            api_key = None
        if not api_key:
            raise ValueError(
                "ElevenLabs TTS requires an API key. "
                "Go to AI Models → Voice Engine and ensure your ElevenLabs connection has an API key."
            )
        # Speech-to-Speech models (e.g. eleven_multilingual_sts_v2) require audio input, not text.
        # Using them with the TTS WebSocket causes a silent pipeline deadlock.
        tts_model = (user_config.tts.model or "").lower()
        if "_sts_" in tts_model:
            raise ValueError(
                f"ElevenLabs model '{user_config.tts.model}' is a Speech-to-Speech model and "
                "cannot be used as a Text-to-Speech engine. "
                "Please select a TTS model such as 'eleven_multilingual_v2' or 'eleven_flash_v2_5'."
            )
        # Backward compatible with older configuration "Name - voice_id"
        try:
            voice_id = user_config.tts.voice.split(" - ")[1]
        except IndexError:
            voice_id = user_config.tts.voice
        logger.info(
            f"ElevenLabs TTS: voice_id={voice_id!r} model={user_config.tts.model!r} "
            f"api_key_set={bool(api_key)} speed={user_config.tts.speed}"
        )
        # ElevenLabs TTS uses WebSocket. Users configure base_url with an HTTP
        # scheme (matching ElevenLabs documentation, e.g.
        # https://api.eu.residency.elevenlabs.io); rewrite it to the WS scheme.
        _validate_runtime_service_url(user_config.tts.base_url, "base_url")
        elevenlabs_url = (
            user_config.tts.base_url
            .rstrip("/")
            .replace("https://", "wss://")
            .replace("http://", "ws://")
        )
        return DograhElevenLabsTTSService(
            reconnect_on_error=True,
            api_key=api_key,
            url=elevenlabs_url,
            settings=ElevenLabsTTSSettings(
                voice=voice_id,
                model=user_config.tts.model,
                stability=0.8,
                speed=user_config.tts.speed,
                similarity_boost=0.75,
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.CARTESIA.value:
        speed = getattr(user_config.tts, "speed", None)
        volume = getattr(user_config.tts, "volume", None)
        gen_config_kwargs = {}
        if speed and speed != 1.0:
            gen_config_kwargs["speed"] = speed
        if volume and volume != 1.0:
            gen_config_kwargs["volume"] = volume
        generation_config = (
            GenerationConfig(**gen_config_kwargs) if gen_config_kwargs else None
        )
        return CartesiaTTSService(
            api_key=user_config.tts.api_key,
            settings=CartesiaTTSSettings(
                voice=user_config.tts.voice,
                model=user_config.tts.model,
                **(
                    {"generation_config": generation_config}
                    if generation_config
                    else {}
                ),
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.DOGRAH.value:
        # Convert HTTP URL to WebSocket URL for TTS
        base_url = MPS_API_URL.replace("http://", "ws://").replace("https://", "wss://")
        return DograhTTSService(
            base_url=base_url,
            api_key=user_config.tts.api_key,
            settings=DograhTTSSettings(
                model=user_config.tts.model,
                voice=user_config.tts.voice,
                speed=user_config.tts.speed,
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.CAMB.value:
        from pipecat.services.camb.tts import CambTTSService

        voice_id = int(getattr(user_config.tts, "voice", None) or "147320")
        language = getattr(user_config.tts, "language", None) or "en-us"
        tts = CambTTSService(
            api_key=user_config.tts.api_key,
            voice_id=voice_id,
            model=user_config.tts.model,
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
        )
        # Set language directly as BCP-47 code (bypasses Language enum conversion)
        tts._settings.language = language
        return tts
    elif user_config.tts.provider == ServiceProviders.SPEACHES.value:
        _validate_runtime_service_url(user_config.tts.base_url, "base_url")
        return SpeachesTTSService(
            base_url=user_config.tts.base_url,
            api_key=user_config.tts.api_key or "none",
            settings=SpeachesTTSSettings(
                model=user_config.tts.model,
                voice=user_config.tts.voice,
                speed=user_config.tts.speed,
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.RIME.value:
        speed = getattr(user_config.tts, "speed", None)
        language_code = getattr(user_config.tts, "language", None) or "en"
        rime_language_mapping = {
            "en": Language.EN,
            "de": Language.DE,
            "fr": Language.FR,
            "es": Language.ES,
            "hi": Language.HI,
        }
        pipecat_language = rime_language_mapping.get(language_code, Language.EN)
        settings_kwargs = {
            "voice": user_config.tts.voice,
            "model": user_config.tts.model,
            "language": pipecat_language,
        }
        if speed and speed != 1.0:
            settings_kwargs["speedAlpha"] = speed
        return RimeTTSService(
            api_key=user_config.tts.api_key,
            settings=RimeTTSSettings(**settings_kwargs),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.SARVAM.value:
        # Map Sarvam language code to pipecat Language enum for TTS
        language_mapping = {
            "bn-IN": Language.BN,
            "en-IN": Language.EN,
            "gu-IN": Language.GU,
            "hi-IN": Language.HI,
            "kn-IN": Language.KN,
            "ml-IN": Language.ML,
            "mr-IN": Language.MR,
            "od-IN": Language.OR,
            "pa-IN": Language.PA,
            "ta-IN": Language.TA,
            "te-IN": Language.TE,
        }
        language = getattr(user_config.tts, "language", None)
        pipecat_language = language_mapping.get(language, Language.HI)

        voice = getattr(user_config.tts, "voice", None) or "anushka"
        return SarvamTTSService(
            api_key=user_config.tts.api_key,
            settings=SarvamTTSSettings(
                model=user_config.tts.model,
                voice=voice,
                language=pipecat_language,
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.MINIMAX.value:
        group_id = getattr(user_config.tts, "group_id", None)
        if not group_id:
            raise HTTPException(
                status_code=400,
                detail="MiniMax TTS requires a group_id. Configure it in your TTS settings.",
            )
        voice = getattr(user_config.tts, "voice", None) or "English_Graceful_Lady"
        speed = getattr(user_config.tts, "speed", None) or 1.0

        # Pipecat appends "?GroupId=..." to base_url as-is, so /t2a_v2 must
        # already be in the path.
        base_url = (
            getattr(user_config.tts, "base_url", None)
            or "https://api.minimax.io/v1/t2a_v2"
        ).rstrip("/")
        if not base_url.endswith("/t2a_v2"):
            base_url = f"{base_url}/t2a_v2"
        _validate_runtime_service_url(base_url, "base_url")

        session = aiohttp.ClientSession()
        return MiniMaxOwnedSessionTTSService(
            api_key=user_config.tts.api_key,
            group_id=group_id,
            base_url=base_url,
            aiohttp_session=session,
            settings=MiniMaxTTSSettings(
                model=user_config.tts.model,
                voice=voice,
                speed=speed,
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.XAI.value:
        _xai_tts_voice = getattr(user_config.tts, "voice", None) or ""
        # "grok-voice-latest" is a model name, not a voice name; reject it
        _VALID_XAI_TTS_VOICES = {"eve", "Ara", "Rex", "Sal", "Leo"}
        voice = _xai_tts_voice if _xai_tts_voice in _VALID_XAI_TTS_VOICES else "eve"
        language_code = getattr(user_config.tts, "language", None) or "en"
        try:
            lang_enum = Language(language_code)
        except ValueError:
            lang_enum = Language.EN
        return XAITTSService(
            api_key=user_config.tts.api_key,
            settings=XAIWebsocketTTSSettings(
                model=user_config.tts.model, voice=voice, language=lang_enum
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.AWS_POLLY.value:
        aws_access_key = getattr(user_config.tts, "aws_access_key", None) or None
        aws_secret_key = getattr(user_config.tts, "aws_secret_key", None) or None
        aws_region = getattr(user_config.tts, "aws_region", None) or "us-east-1"
        voice = getattr(user_config.tts, "voice", None) or "Joanna"
        engine = getattr(user_config.tts, "model", None) or "neural"
        language = getattr(user_config.tts, "language", None) or "en-US"
        if not aws_access_key or not aws_secret_key:
            raise HTTPException(
                status_code=400,
                detail="AWS Polly requires aws_access_key and aws_secret_key. Configure them in your TTS settings.",
            )
        try:
            lang_enum = Language(language)
        except ValueError:
            lang_enum = Language.EN_US
        return AWSPollyTTSService(
            aws_access_key_id=aws_access_key,
            api_key=aws_secret_key,
            region=aws_region,
            settings=AWSPollyTTSSettings(
                voice=voice,
                language=lang_enum,
                engine=engine if engine in ("standard", "neural", "long-form", "generative") else "neural",
            ),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.AZURE_TTS.value:
        api_key = user_config.tts.api_key
        region = getattr(user_config.tts, "region", None) or "eastus"
        voice = getattr(user_config.tts, "voice", None) or "en-US-JennyNeural"
        language = getattr(user_config.tts, "language", None) or "en-US"
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail="Azure TTS requires an API key (Azure Cognitive Services subscription key).",
            )
        try:
            lang_enum = Language(language)
        except ValueError:
            lang_enum = Language.EN_US
        return AzureTTSService(
            api_key=api_key,
            region=region,
            settings=AzureTTSSettings(voice=voice, language=lang_enum),
            text_filters=[xml_function_tag_filter],
            skip_aggregator_types=["recording_router", "recording"],
            silence_time_s=1.0,
        )
    elif user_config.tts.provider == ServiceProviders.PLAYHT.value:
        import aiohttp as _aiohttp

        api_key = user_config.tts.api_key
        user_id = getattr(user_config.tts, "user_id", None)
        voice = getattr(user_config.tts, "voice", None) or ""
        model = getattr(user_config.tts, "model", None) or "Play3.0-mini"
        speed = getattr(user_config.tts, "speed", None) or 1.0
        if not api_key or not user_id:
            raise HTTPException(
                status_code=400,
                detail="PlayHT requires both an API key and a User ID. Configure them in your TTS settings.",
            )

        async def _playht_tts_generator(text_queue):
            """Minimal inline PlayHT TTS via REST — returns audio bytes."""
            session = _aiohttp.ClientSession()
            try:
                payload = {
                    "text": "",
                    "voice": voice,
                    "output_format": "wav",
                    "voice_engine": model,
                    "speed": speed,
                }
                async with session.post(
                    "https://api.play.ht/api/v2/tts/stream",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "X-User-ID": user_id,
                        "Content-Type": "application/json",
                        "Accept": "audio/wav",
                    },
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise ValueError(f"PlayHT API error {resp.status}: {error[:200]}")
                    return await resp.read()
            finally:
                await session.close()

        # Use OpenAI-compatible TTS fallback via OpenAI service with PlayHT's API
        # PlayHT doesn't have an OpenAI-compatible wrapper, so raise a helpful error
        # if direct HTTP calls aren't available through the existing Pipecat service set.
        raise HTTPException(
            status_code=501,
            detail=(
                "PlayHT real-time streaming TTS is not yet supported in the pipeline runtime. "
                "Use ElevenLabs, Azure TTS, or AWS Polly for production voice calls. "
                "PlayHT is available for model/pricing configuration only."
            ),
        )
    elif user_config.tts.provider == ServiceProviders.NEETS.value:
        raise HTTPException(
            status_code=501,
            detail=(
                "Neets.ai real-time streaming TTS is not yet supported in the pipeline runtime. "
                "Use ElevenLabs, Deepgram, or Cartesia for production voice calls. "
                "Neets.ai is available for model/pricing configuration only."
            ),
        )
    else:
        raise HTTPException(
            status_code=400, detail=f"Invalid TTS provider {user_config.tts.provider}"
        )


def create_llm_service_from_provider(
    provider: str,
    model: str,
    api_key: str | None,
    *,
    base_url: str | None = None,
    endpoint: str | None = None,
    aws_access_key: str | None = None,
    aws_secret_key: str | None = None,
    aws_region: str | None = None,
    project_id: str | None = None,
    location: str | None = None,
    credentials: str | None = None,
    temperature: float | None = None,
):
    """Create an LLM service from explicit provider/model/api_key.

    Also used by create_llm_service which extracts these from user_config.
    """
    logger.info(f"Creating LLM service: provider={provider}, model={model}")
    if provider == ServiceProviders.OPENAI.value:
        kwargs = {}
        if base_url:
            _validate_runtime_service_url(base_url, "base_url")
            kwargs["base_url"] = base_url
        if "gpt-5" in model:
            return OpenAILLMService(
                api_key=api_key,
                settings=OpenAILLMSettings(
                    model=model,
                    extra={"reasoning_effort": "minimal", "verbosity": "low"},
                ),
                **kwargs,
            )
        # o-series models (o1, o3, o4-mini, etc.) only accept temperature=1 (default).
        # Detect by checking if the base model name starts with "o" + digit.
        _base = model.split("/")[-1]
        if re.match(r"^o\d", _base):
            return OpenAILLMService(
                api_key=api_key,
                settings=OpenAILLMSettings(model=model),
                **kwargs,
            )
        return OpenAILLMService(
            api_key=api_key,
            settings=OpenAILLMSettings(model=model, temperature=0.1),
            **kwargs,
        )
    elif provider == ServiceProviders.ANTHROPIC.value:
        from api.services.pipecat.anthropic_llm import DograhAnthropicLLMService
        from pipecat.services.anthropic.llm import AnthropicLLMSettings
        return DograhAnthropicLLMService(
            api_key=api_key,
            settings=AnthropicLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.GROQ.value:
        return GroqLLMService(
            api_key=api_key,
            settings=GroqLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.XAI.value:
        xai_base_url = base_url or "https://api.x.ai/v1"
        _validate_runtime_service_url(xai_base_url, "base_url")
        return OpenAILLMService(
            api_key=api_key,
            base_url=xai_base_url,
            settings=OpenAILLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.OPENROUTER.value:
        kwargs = {}
        if base_url:
            _validate_runtime_service_url(base_url, "base_url")
            kwargs["base_url"] = base_url
        return OpenRouterLLMService(
            api_key=api_key,
            settings=OpenRouterLLMSettings(model=model, temperature=0.1),
            **kwargs,
        )
    elif provider == ServiceProviders.GOOGLE.value:
        return GoogleLLMService(
            api_key=api_key,
            settings=GoogleLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.GOOGLE_VERTEX.value:
        return GoogleVertexLLMService(
            credentials=credentials,
            project_id=project_id,
            location=location or "us-east4",
            settings=GoogleVertexLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.AZURE.value:
        if endpoint:
            _validate_runtime_service_url(endpoint, "endpoint")
        return AzureLLMService(
            api_key=api_key,
            endpoint=endpoint,
            settings=AzureLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.DOGRAH.value:
        return DograhLLMService(
            base_url=f"{MPS_API_URL}/api/v1/llm",
            api_key=api_key,
            settings=OpenAILLMSettings(model=model),
        )
    elif provider == ServiceProviders.AWS_BEDROCK.value:
        return AWSBedrockLLMService(
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            aws_region=aws_region,
            settings=AWSBedrockLLMSettings(model=model),
        )
    elif provider == ServiceProviders.SPEACHES.value:
        base_url = base_url or "http://localhost:11434/v1"
        _validate_runtime_service_url(base_url, "base_url")
        return SpeachesLLMService(
            base_url=base_url,
            api_key=api_key or "none",
            settings=SpeachesLLMSettings(model=model),
        )
    elif provider == ServiceProviders.MINIMAX.value:
        base_url = base_url or "https://api.minimax.io/v1"
        _validate_runtime_service_url(base_url, "base_url")
        return DograhMiniMaxLLMService(
            api_key=api_key,
            base_url=base_url,
            settings=DograhMiniMaxLLMService.Settings(
                model=model,
                temperature=temperature if temperature is not None else 1.0,
            ),
        )
    elif provider == ServiceProviders.MISTRAL.value:
        mistral_base_url = base_url or "https://api.mistral.ai/v1"
        _validate_runtime_service_url(mistral_base_url, "base_url")
        return MistralLLMService(
            api_key=api_key,
            base_url=mistral_base_url,
            settings=MistralLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.TOGETHER.value:
        together_base_url = base_url or "https://api.together.xyz/v1"
        _validate_runtime_service_url(together_base_url, "base_url")
        return TogetherLLMService(
            api_key=api_key,
            base_url=together_base_url,
            settings=TogetherLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.CEREBRAS.value:
        cerebras_base_url = base_url or "https://api.cerebras.ai/v1"
        _validate_runtime_service_url(cerebras_base_url, "base_url")
        return CerebrasLLMService(
            api_key=api_key,
            base_url=cerebras_base_url,
            settings=CerebrasLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.FIREWORKS.value:
        fireworks_base_url = base_url or "https://api.fireworks.ai/inference/v1"
        _validate_runtime_service_url(fireworks_base_url, "base_url")
        return FireworksLLMService(
            api_key=api_key,
            base_url=fireworks_base_url,
            settings=FireworksLLMSettings(model=model, temperature=0.1),
        )
    elif provider == ServiceProviders.COHERE.value:
        cohere_base_url = base_url or "https://api.cohere.com/compatibility/v1"
        _validate_runtime_service_url(cohere_base_url, "base_url")
        return OpenAILLMService(
            api_key=api_key,
            base_url=cohere_base_url,
            settings=OpenAILLMSettings(model=model, temperature=0.1),
        )
    else:
        raise HTTPException(status_code=400, detail=f"Invalid LLM provider {provider}")


def create_realtime_llm_service(user_config, audio_config: "AudioConfig"):
    """Create a realtime (speech-to-speech) LLM service that handles STT+LLM+TTS.

    These services bypass separate STT/TTS and handle audio directly via
    a bidirectional WebSocket connection. Reads from user_config.realtime.
    """
    realtime_config = user_config.realtime
    provider = realtime_config.provider
    model = realtime_config.model
    api_key = realtime_config.api_key
    voice = getattr(realtime_config, "voice", None)
    language = getattr(realtime_config, "language", None)

    logger.info(
        f"Creating realtime LLM service: provider={provider}, model={model}, voice={voice}, language={language}"
    )

    if provider == ServiceProviders.OPENAI_REALTIME.value:
        from api.services.pipecat.realtime.openai_realtime import (
            DograhOpenAIRealtimeLLMService,
        )
        from pipecat.services.openai.realtime.events import (
            AudioConfiguration,
            AudioInput,
            AudioOutput,
            InputAudioTranscription,
            SessionProperties,
        )

        return DograhOpenAIRealtimeLLMService(
            api_key=api_key,
            settings=DograhOpenAIRealtimeLLMService.Settings(
                model=model,
                session_properties=SessionProperties(
                    audio=AudioConfiguration(
                        input=AudioInput(
                            transcription=InputAudioTranscription(),
                        ),
                        output=AudioOutput(
                            voice=voice or "alloy",
                        ),
                    ),
                ),
            ),
        )
    elif provider == ServiceProviders.GROK_REALTIME.value:
        from api.services.pipecat.realtime.grok_realtime import (
            DograhGrokRealtimeLLMService,
        )
        from pipecat.services.xai.realtime.events import SessionProperties

        return DograhGrokRealtimeLLMService(
            api_key=api_key,
            settings=DograhGrokRealtimeLLMService.Settings(
                model=model,
                session_properties=SessionProperties(
                    voice=voice or "Ara",
                ),
            ),
        )
    elif provider == ServiceProviders.ULTRAVOX_REALTIME.value:
        from api.services.pipecat.realtime.ultravox_realtime import (
            DograhUltravoxOneShotInputParams,
            DograhUltravoxRealtimeLLMService,
        )

        return DograhUltravoxRealtimeLLMService(
            params=DograhUltravoxOneShotInputParams(
                api_key=api_key,
                model=model,
                voice=voice,
                output_medium="voice",
            ),
            settings=DograhUltravoxRealtimeLLMService.Settings(
                model=model,
                output_medium="voice",
            ),
        )
    elif provider == ServiceProviders.GOOGLE_REALTIME.value:
        from api.services.pipecat.realtime.gemini_live import (
            DograhGeminiLiveLLMService,
        )

        # Gemini Live enables input/output audio transcription by default
        # in its _connect() method — no need to configure it explicitly.
        settings_kwargs = {
            "model": model,
            "voice": voice or "Puck",
        }
        if language:
            settings_kwargs["language"] = language
        return DograhGeminiLiveLLMService(
            api_key=api_key,
            settings=DograhGeminiLiveLLMService.Settings(**settings_kwargs),
        )
    elif provider == ServiceProviders.GOOGLE_VERTEX_REALTIME.value:
        from api.services.pipecat.realtime.gemini_live_vertex import (
            DograhGeminiLiveVertexLLMService,
        )

        project_id = getattr(realtime_config, "project_id", None)
        location = getattr(realtime_config, "location", None) or "us-east4"
        credentials = getattr(realtime_config, "credentials", None)
        if credentials and contains_masked_key(str(credentials)):
            credentials = None

        settings_kwargs = {
            "model": model,
            "voice": voice or "Charon",
        }
        if language:
            settings_kwargs["language"] = language
        return DograhGeminiLiveVertexLLMService(
            credentials=credentials,
            project_id=project_id,
            location=location,
            settings=DograhGeminiLiveVertexLLMService.Settings(**settings_kwargs),
        )
    else:
        raise HTTPException(
            status_code=400, detail=f"Invalid realtime LLM provider {provider}"
        )


def create_llm_service(user_config):
    """Create and return appropriate LLM service based on user configuration."""
    provider = user_config.llm.provider
    model = user_config.llm.model
    api_key = user_config.llm.api_key

    kwargs = {}
    if provider == ServiceProviders.OPENAI.value:
        kwargs["base_url"] = user_config.llm.base_url
    elif provider == ServiceProviders.OPENROUTER.value:
        kwargs["base_url"] = user_config.llm.base_url
    elif provider == ServiceProviders.AZURE.value:
        kwargs["endpoint"] = user_config.llm.endpoint
    elif provider == ServiceProviders.XAI.value:
        kwargs["base_url"] = user_config.llm.base_url
    elif provider == ServiceProviders.SPEACHES.value:
        kwargs["base_url"] = user_config.llm.base_url
    elif provider == ServiceProviders.AWS_BEDROCK.value:
        kwargs["aws_access_key"] = user_config.llm.aws_access_key
        kwargs["aws_secret_key"] = user_config.llm.aws_secret_key
        kwargs["aws_region"] = user_config.llm.aws_region
    elif provider == ServiceProviders.GOOGLE_VERTEX.value:
        kwargs["project_id"] = user_config.llm.project_id
        kwargs["location"] = user_config.llm.location
        creds = user_config.llm.credentials
        if creds and contains_masked_key(str(creds)):
            creds = None
        kwargs["credentials"] = creds
    elif provider == ServiceProviders.MINIMAX.value:
        kwargs["base_url"] = user_config.llm.base_url
        kwargs["temperature"] = user_config.llm.temperature
    elif provider in (
        ServiceProviders.MISTRAL.value,
        ServiceProviders.TOGETHER.value,
        ServiceProviders.CEREBRAS.value,
        ServiceProviders.FIREWORKS.value,
        ServiceProviders.COHERE.value,
    ):
        kwargs["base_url"] = user_config.llm.base_url

    return create_llm_service_from_provider(provider, model, api_key, **kwargs)
