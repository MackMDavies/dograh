from unittest.mock import patch

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
