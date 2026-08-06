"""voice_settings must reach the provider's own config fields.

The UI writes tuning to model_overrides.tts.voice_settings; nothing consumed
it, so every slider was inert. These tests pin the translation.
"""

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.resolve import resolve_effective_config
from api.services.configuration.voice_settings import flatten_voice_settings


class TestFlattenVoiceSettings:
    def test_fish_speed_maps_to_prosody_speed(self):
        out = flatten_voice_settings({
            "provider": "fish",
            "voice_settings": {"speed": 1.4, "volume_gain_db": -3, "temperature": 0.6},
        })
        assert out["prosody_speed"] == 1.4
        assert out["prosody_volume"] == -3
        assert out["temperature"] == 0.6

    def test_fish_latency_and_top_p_map_through(self):
        out = flatten_voice_settings({
            "provider": "fish",
            "voice_settings": {"latency": "normal", "top_p": 0.85},
        })
        assert out["latency"] == "normal"
        assert out["top_p"] == 0.85

    def test_latency_is_fish_only(self):
        # ElevenLabs has no latency knob — it must not leak onto the config.
        out = flatten_voice_settings({
            "provider": "elevenlabs",
            "voice_settings": {"speed": 1.0, "latency": "normal"},
        })
        assert "latency" not in out

    def test_elevenlabs_keeps_native_names(self):
        out = flatten_voice_settings({
            "provider": "elevenlabs",
            "voice_settings": {"speed": 1.1, "stability": 0.3, "use_speaker_boost": True},
        })
        assert out["speed"] == 1.1
        assert out["stability"] == 0.3
        assert out["use_speaker_boost"] is True

    def test_unsupported_key_for_provider_is_dropped(self):
        # OpenAI TTS has no stability knob — it must not leak through.
        out = flatten_voice_settings({
            "provider": "openai",
            "voice_settings": {"speed": 2.0, "stability": 0.9},
        })
        assert out["speed"] == 2.0
        assert "stability" not in out

    def test_explicit_top_level_value_wins(self):
        out = flatten_voice_settings({
            "provider": "fish",
            "prosody_speed": 0.8,
            "voice_settings": {"speed": 1.9},
        })
        assert out["prosody_speed"] == 0.8

    def test_unknown_provider_passes_through_untouched(self):
        override = {"provider": "aws_polly", "voice_settings": {"speed": 1.5}}
        assert flatten_voice_settings(override) == override

    def test_no_voice_settings_is_a_noop(self):
        override = {"provider": "fish", "prosody_speed": 1.2}
        assert flatten_voice_settings(override) == override


class TestResolveAppliesVoiceSettings:
    def _base_fish_config(self):
        return UserConfiguration.model_validate({
            "tts": {"provider": "fish", "api_key": "k", "model": "s1", "voice": "v1"},
        })

    def test_override_tuning_reaches_the_typed_config(self):
        effective = resolve_effective_config(
            self._base_fish_config(),
            {"tts": {"voice_settings": {"speed": 1.6, "volume_gain_db": 4}}},
        )
        assert effective.tts.prosody_speed == 1.6
        assert effective.tts.prosody_volume == 4

    def test_fish_expressiveness_and_latency_reach_the_config(self):
        effective = resolve_effective_config(
            self._base_fish_config(),
            {"tts": {"voice_settings": {"temperature": 0.35, "latency": "normal"}}},
        )
        assert effective.tts.temperature == 0.35
        assert effective.tts.latency == "normal"

    def test_tuning_applies_when_provider_also_changes(self):
        effective = resolve_effective_config(
            self._base_fish_config(),
            {
                "tts": {
                    "provider": "elevenlabs",
                    "api_key": "k2",
                    "voice": "abc",
                    "voice_settings": {"speed": 1.05, "stability": 0.2},
                }
            },
        )
        assert effective.tts.provider.value == "elevenlabs"
        assert effective.tts.speed == 1.05
        assert effective.tts.stability == 0.2
