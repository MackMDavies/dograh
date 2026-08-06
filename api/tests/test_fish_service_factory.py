from types import SimpleNamespace
from unittest.mock import patch

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.service_factory import create_tts_service


def _fish_user_config(**overrides):
    defaults = dict(
        provider=ServiceProviders.FISH.value,
        api_key="test-key",
        model="s2-pro",
        voice="abc123",
        latency="balanced",
        normalize=True,
        temperature=None,
        top_p=None,
        prosody_speed=1.0,
        prosody_volume=0,
        output_format="pcm",
    )
    defaults.update(overrides)
    return SimpleNamespace(tts=SimpleNamespace(**defaults))


class TestFishTTSServiceFactory:
    def test_create_fish_tts_service_default_settings(self):
        user_config = _fish_user_config()
        audio_config = SimpleNamespace(transport_in_sample_rate=16000)

        with patch(
            "api.services.pipecat.service_factory.FishAudioTTSService"
        ) as mock_service:
            create_tts_service(user_config, audio_config)

        assert mock_service.call_count == 1
        kwargs = mock_service.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["output_format"] == "pcm"
        settings = kwargs["settings"]
        assert settings.model == "s2-pro"
        assert settings.voice == "abc123"
        assert settings.latency == "balanced"
        assert settings.prosody_speed == 1.0
        assert settings.prosody_volume == 0

    def test_create_fish_tts_service_passes_temperature_and_top_p_when_set(self):
        user_config = _fish_user_config(temperature=0.6, top_p=0.9)
        audio_config = SimpleNamespace(transport_in_sample_rate=16000)

        with patch(
            "api.services.pipecat.service_factory.FishAudioTTSService"
        ) as mock_service:
            create_tts_service(user_config, audio_config)

        settings = mock_service.call_args.kwargs["settings"]
        assert settings.temperature == 0.6
        assert settings.top_p == 0.9

    def test_create_fish_tts_service_omits_temperature_and_top_p_when_none(self):
        from pipecat.services.settings import NOT_GIVEN

        user_config = _fish_user_config(temperature=None, top_p=None)
        audio_config = SimpleNamespace(transport_in_sample_rate=16000)

        with patch(
            "api.services.pipecat.service_factory.FishAudioTTSService"
        ) as mock_service:
            create_tts_service(user_config, audio_config)

        # FishAudioTTSSettings leaves unset optional fields as the NOT_GIVEN
        # sentinel rather than None — assert against that directly.
        settings = mock_service.call_args.kwargs["settings"]
        assert settings.temperature is NOT_GIVEN
        assert settings.top_p is NOT_GIVEN
