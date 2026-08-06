from api.services.configuration.registry import (
    FishTTSConfiguration,
    ServiceProviders,
    TTSConfig,
)


class TestFishTTSConfiguration:
    def test_default_values(self):
        config = FishTTSConfiguration(api_key="test-key")
        assert config.provider == ServiceProviders.FISH
        assert config.model == "s2-pro"
        assert config.voice is None
        assert config.latency == "balanced"
        assert config.normalize is True
        assert config.temperature is None
        assert config.top_p is None
        assert config.prosody_speed == 1.0
        assert config.prosody_volume == 0
        assert config.output_format == "pcm"

    def test_custom_values(self):
        config = FishTTSConfiguration(
            api_key="test-key",
            model="s1",
            voice="abc123",
            latency="normal",
            temperature=0.5,
            top_p=0.8,
            prosody_speed=1.2,
            prosody_volume=5,
        )
        assert config.model == "s1"
        assert config.voice == "abc123"
        assert config.latency == "normal"
        assert config.temperature == 0.5
        assert config.top_p == 0.8
        assert config.prosody_speed == 1.2
        assert config.prosody_volume == 5

    def test_registered_in_tts_union(self):
        # Discriminated union construction must resolve to FishTTSConfiguration
        # when provider == "fish".
        from pydantic import TypeAdapter

        adapter = TypeAdapter(TTSConfig)
        result = adapter.validate_python({"provider": "fish", "api_key": "test-key"})
        assert isinstance(result, FishTTSConfiguration)
