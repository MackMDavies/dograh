from types import SimpleNamespace
from unittest.mock import patch

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.service_factory import create_tts_service


def _elevenlabs_config(model: str):
    return SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.ELEVENLABS.value,
            api_key="test-elevenlabs-key",
            base_url="https://api.elevenlabs.io",
            model=model,
            voice="test-voice-id",
            speed=1.0,
            stability=0.8,
            similarity_boost=0.75,
            style=0.0,
            use_speaker_boost=False,
        )
    )


def _audio_config():
    return SimpleNamespace(pipeline_sample_rate=16000)


def test_eleven_v4_uses_http_streaming_service():
    with (
        patch("api.services.pipecat.service_factory.aiohttp.ClientSession") as session,
        patch(
            "api.services.pipecat.service_factory.DograhElevenLabsHttpTTSService"
        ) as http_service,
        patch(
            "api.services.pipecat.service_factory.DograhElevenLabsTTSService"
        ) as websocket_service,
    ):
        create_tts_service(_elevenlabs_config("eleven_v4"), _audio_config())

    assert http_service.call_count == 1
    assert websocket_service.call_count == 0
    assert http_service.call_args.kwargs["settings"].model == "eleven_v4"
    assert http_service.call_args.kwargs["settings"].voice == "test-voice-id"
    assert http_service.call_args.kwargs["aiohttp_session"] is session.return_value


def test_eleven_flash_keeps_websocket_service():
    with (
        patch("api.services.pipecat.service_factory.aiohttp.ClientSession") as session,
        patch(
            "api.services.pipecat.service_factory.DograhElevenLabsHttpTTSService"
        ) as http_service,
        patch(
            "api.services.pipecat.service_factory.DograhElevenLabsTTSService"
        ) as websocket_service,
    ):
        create_tts_service(
            _elevenlabs_config("eleven_flash_v2_5"), _audio_config()
        )

    assert websocket_service.call_count == 1
    assert http_service.call_count == 0
    assert websocket_service.call_args.kwargs["settings"].model == "eleven_flash_v2_5"
    assert websocket_service.call_args.kwargs["url"] == "wss://api.elevenlabs.io"
    session.assert_not_called()
