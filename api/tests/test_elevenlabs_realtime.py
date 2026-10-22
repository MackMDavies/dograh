from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioTranscription,
    SessionProperties,
)

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import (
    ELEVENLABS_REALTIME_MODELS,
    REGISTRY,
    ServiceType,
)
from api.services.pipecat.realtime.elevenlabs_realtime import (
    DograhElevenLabsRealtimeLLMService,
)
from api.services.pipecat.service_factory import create_realtime_llm_service
from api.services.workflow.pipecat_engine import PipecatEngine
from pipecat.processors.aggregators.llm_context import LLMContext


@pytest.mark.parametrize("model", ELEVENLABS_REALTIME_MODELS)
def test_elevenlabs_realtime_configuration_is_registered(model):
    config = UserConfiguration.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "llm-key"},
            "is_realtime": True,
            "realtime": {
                "provider": "elevenlabs_realtime",
                "model": model,
                "voice": "brian",
                "api_key": "el-key",
            },
        }
    )

    assert config.realtime.model == model
    assert config.realtime.voice == "brian"
    assert REGISTRY[ServiceType.REALTIME]["elevenlabs_realtime"]


def test_elevenlabs_realtime_factory_uses_compatible_endpoint_and_voice():
    config = UserConfiguration.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "llm-key"},
            "is_realtime": True,
            "realtime": {
                "provider": "elevenlabs_realtime",
                "model": "eleven_realtime_v1_max",
                "voice": "voice-id-123",
                "api_key": "el-key",
            },
        }
    )

    with patch(
        "api.services.pipecat.realtime.elevenlabs_realtime.DograhElevenLabsRealtimeLLMService"
    ) as service_cls:
        service_cls.Settings = DograhElevenLabsRealtimeLLMService.Settings
        result = create_realtime_llm_service(config, audio_config=None)

    assert result is service_cls.return_value
    kwargs = service_cls.call_args.kwargs
    assert kwargs["api_key"] == "el-key"
    assert kwargs["base_url"] == "wss://api.elevenlabs.io/v1/convai/realtime"
    assert kwargs["settings"].model == "eleven_realtime_v1_max"
    assert kwargs["settings"].session_properties.audio.output.voice == "voice-id-123"


def test_elevenlabs_realtime_factory_uses_voice_engine_selection():
    config = UserConfiguration.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "llm-key"},
            "tts": {
                "provider": "elevenlabs",
                "model": "eleven_v4",
                "voice": "voice-engine-id",
                "api_key": "tts-key",
            },
            "is_realtime": True,
            "realtime": {
                "provider": "elevenlabs_realtime",
                "model": "eleven_realtime_v1_max",
                "voice": "brian",
                "api_key": "el-key",
            },
        }
    )

    with patch(
        "api.services.pipecat.realtime.elevenlabs_realtime.DograhElevenLabsRealtimeLLMService"
    ) as service_cls:
        service_cls.Settings = DograhElevenLabsRealtimeLLMService.Settings
        create_realtime_llm_service(config, audio_config=None)

    assert (
        service_cls.call_args.kwargs["settings"].session_properties.audio.output.voice
        == "voice-engine-id"
    )


def test_client_secret_config_uses_pcm24k_model_and_selected_voice():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="secret-never-assert-or-log",
        base_url="wss://api.elevenlabs.io/v1/convai/realtime",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(
                audio=AudioConfiguration(
                    input=AudioInput(transcription=InputAudioTranscription()),
                    output=AudioOutput(voice="brian"),
                ),
            ),
        ),
    )

    session = service._client_secret_session()

    assert session["type"] == "realtime"
    assert session["model"] == "eleven_realtime_v1_mini"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert service._settings.session_properties.audio.input.transcription is None
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm"}
    assert session["audio"]["output"]["voice"] == "brian"


@pytest.mark.asyncio
async def test_session_ready_starts_an_initial_response_when_opening_frame_was_missed():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(),
        ),
    )
    context = object()
    service._context = context

    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service._handle_evt_session_updated(object())

    create_response.assert_awaited_once_with()
    assert service._handled_initial_context is True
    assert service._context is context
    assert service._initial_response_requested is True


@pytest.mark.asyncio
async def test_session_ready_does_not_duplicate_a_deferred_initial_response():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(),
        ),
    )
    service._context = object()
    service._run_llm_when_api_session_ready = True
    fallback_context = AsyncMock()
    service._handle_context = fallback_context

    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service._handle_evt_session_updated(object())

    create_response.assert_awaited_once_with()
    fallback_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_context_replaces_realtime_services_empty_context():
    engine = object.__new__(PipecatEngine)
    live_context = LLMContext()
    engine.context = live_context
    engine.llm = SimpleNamespace(
        _context=LLMContext(),
        _update_settings=AsyncMock(),
    )

    await engine._update_llm_context("Agent instructions", [])

    assert engine.llm._context is live_context
    engine.llm._update_settings.assert_awaited_once()
