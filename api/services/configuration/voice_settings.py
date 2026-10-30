"""Flatten canonical voice_settings onto provider-specific config fields.

The UI stores voice tuning under a single canonical shape
(`model_overrides.tts.voice_settings`), but every provider names its knobs
differently — ElevenLabs has `stability`, Fish Audio calls speed
`prosody_speed`, Cartesia takes a `volume` multiplier, and so on. Nothing
consumed `voice_settings`, so every slider in the UI was inert.

This module translates the canonical keys onto the fields each provider's
config class actually declares. A canonical key with no equivalent for the
selected provider is dropped rather than guessed at — the provider simply
doesn't support it.
"""

from typing import Any, Dict

from api.services.configuration.registry import ServiceProviders


def build_expressive_mode_instruction(tts_config, realtime_config=None) -> str | None:
    """Build prompt guidance that makes the expressive setting affect speech.

    ElevenLabs' direct TTS API receives expressive tags as part of the generated
    text. Tags are useful only when the language model is told to emit them in
    context, so the selected tags are exposed to the node prompt instead of
    being stamped onto every sentence by the TTS adapter.
    """
    if tts_config is None:
        return None

    provider = getattr(tts_config, "provider", None)
    model = getattr(tts_config, "model", None) or ""
    is_elevenlabs = provider == ServiceProviders.ELEVENLABS.value
    # Expressive text tags are part of ElevenLabs TTS requests; Realtime is a
    # separate speech-to-speech API and must not receive literal TTS tags.
    if not is_elevenlabs or realtime_config is not None or model not in {"eleven_v3", "eleven_v3_conversational", "eleven_v4", "eleven_v4_turbo"}:
        return None

    expressive_setting = getattr(tts_config, "expressive_mode", None)
    expressive_enabled = (
        model == "eleven_v4_turbo"
        if expressive_setting is None
        else bool(expressive_setting)
    )
    if not expressive_enabled:
        return None

    tags = getattr(tts_config, "suggested_audio_tags", None) or []
    valid_tags = [
        tag.strip().lower()
        for tag in tags
        if isinstance(tag, str) and tag.strip()
    ]
    tag_guidance = (
        "Use only these suggested expressive tags when they genuinely fit: "
        + ", ".join(f"[{tag}]" for tag in valid_tags)
        + "."
        if valid_tags
        else "Use suitable ElevenLabs expressive audio tags, such as [laughs], "
        "[sighs], [whispers], [excited], or [hesitant], only when they fit."
    )
    return (
        "VOICE DELIVERY (ElevenLabs expressive mode): Speak naturally and adapt "
        "your emotional delivery to the conversation. Do not read tag names aloud. "
        "Use expressive audio tags sparingly and only at the moment they apply; "
        "never prefix every response with a tag. "
        + tag_guidance
    )

# canonical voice_settings key -> provider config field name.
# Only list a mapping when the provider's config class declares that field AND
# the underlying pipecat service actually accepts it; anything absent here is
# intentionally unsupported for that provider.
_SPEED_ONLY = {"speed": "speed"}

VOICE_SETTINGS_MAP: Dict[str, Dict[str, str]] = {
    ServiceProviders.ELEVENLABS.value: {
        "speed": "speed",
        "stability": "stability",
        "similarity_boost": "similarity_boost",
        "style": "style",
        "use_speaker_boost": "use_speaker_boost",
        "expressive_mode": "expressive_mode",
        "suggested_audio_tags": "suggested_audio_tags",
    },
    ServiceProviders.FISH.value: {
        "speed": "prosody_speed",
        "volume_gain_db": "prosody_volume",
        "temperature": "temperature",
        "top_p": "top_p",
        "latency": "latency",
    },
    ServiceProviders.CARTESIA.value: {"speed": "speed", "volume": "volume"},
    ServiceProviders.OPENAI.value: _SPEED_ONLY,
    ServiceProviders.GOOGLE.value: _SPEED_ONLY,
    ServiceProviders.RIME.value: _SPEED_ONLY,
    ServiceProviders.MINIMAX.value: _SPEED_ONLY,
    ServiceProviders.DOGRAH.value: _SPEED_ONLY,
    ServiceProviders.SPEACHES.value: _SPEED_ONLY,
    ServiceProviders.PLAYHT.value: _SPEED_ONLY,
    ServiceProviders.AZURE_TTS.value: _SPEED_ONLY,
}


def flatten_voice_settings(override: Dict[str, Any]) -> Dict[str, Any]:
    """Return *override* with `voice_settings` flattened onto provider fields.

    The canonical `voice_settings` block is left in place (harmless, and other
    callers still read it) — the flattened fields are what the service factory
    consumes. Explicit top-level values already on the override win, so a
    caller can still pin a field directly.
    """
    settings = override.get("voice_settings")
    if not isinstance(settings, dict) or not settings:
        return override

    mapping = VOICE_SETTINGS_MAP.get(override.get("provider") or "")
    if not mapping:
        return override

    result = dict(override)
    for canonical, target_field in mapping.items():
        value = settings.get(canonical)
        if value is None:
            continue
        # Don't clobber a value explicitly set on the override itself.
        if result.get(target_field) is None:
            result[target_field] = value
    return result
