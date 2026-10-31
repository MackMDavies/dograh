"""Shared native configuration resolution, before any provider SDK is started.

Keep live calls and private planning on the same precedence rules. This module
does not construct providers, write configuration or start a call.
"""


async def resolve_pipeline_configuration(db, user_id, organization_id, model_overrides):
    from api.services.configuration.org_provider_resolver import (
        enrich_config_from_org_connections,
        enrich_overrides_with_org_api_keys,
        resolve_org_provider_config,
    )
    from api.services.configuration.resolve import resolve_effective_config

    config = await db.get_user_configurations(user_id)
    if organization_id:
        config = await resolve_org_provider_config(organization_id, config)
        config = await enrich_config_from_org_connections(organization_id, config)
    overrides = model_overrides
    if overrides and organization_id:
        overrides = await enrich_overrides_with_org_api_keys(overrides, organization_id)
    return resolve_effective_config(config, overrides)


def apply_pipeline_call_configuration(config, workflow_configurations, call_context):
    """Apply the existing Syra per-call rate before resolving the selected voice."""
    if config.tts is None:
        return config
    from api.services.syra_voice import is_syra_voice_config, syra_speech_speed

    if not is_syra_voice_config(workflow_configurations):
        return config
    speed = syra_speech_speed(call_context.get("syra_speech_speed"))
    provider = getattr(config.tts.provider, "value", config.tts.provider)
    if speed is None or provider != "elevenlabs":
        return config
    effective = config.model_copy(deep=True)
    effective.tts = effective.tts.model_copy(update={"speed": speed})
    return effective


_LANGUAGE_NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "bn": "Bengali",
    "bg": "Bulgarian", "ca": "Catalan", "zh": "Mandarin Chinese", "yue": "Cantonese",
    "hr": "Croatian", "cs": "Czech", "da": "Danish", "nl": "Dutch",
    "en": "English", "et": "Estonian", "fi": "Finnish", "fr": "French",
    "de": "German", "el": "Greek", "gu": "Gujarati", "he": "Hebrew",
    "hi": "Hindi", "hu": "Hungarian", "is": "Icelandic", "id": "Indonesian",
    "it": "Italian", "ja": "Japanese", "kn": "Kannada", "ko": "Korean",
    "lv": "Latvian", "lt": "Lithuanian", "ms": "Malay", "ml": "Malayalam",
    "mr": "Marathi", "no": "Norwegian", "nb": "Norwegian", "pa": "Punjabi",
    "fa": "Persian", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sr": "Serbian", "sk": "Slovak", "sl": "Slovenian",
    "es": "Spanish", "sw": "Swahili", "sv": "Swedish", "tl": "Tagalog",
    "fil": "Filipino", "ta": "Tamil", "te": "Telugu", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese",
    "cy": "Welsh", "eu": "Basque", "yo": "Yoruba", "zu": "Zulu",
}
_LANGUAGE_LABELS = {
    "english": "en", "english (uk)": "en-GB", "english (us)": "en-US",
    "spanish": "es", "french": "fr", "polish": "pl", "urdu": "ur",
    "german": "de", "italian": "it", "portuguese": "pt", "dutch": "nl",
    "arabic": "ar", "hindi": "hi", "japanese": "ja", "korean": "ko",
    "chinese": "zh", "mandarin": "zh", "cantonese": "yue",
}

_DEEPGRAM_FLUX_MULTILINGUAL_LANGUAGES = {
    "en", "es", "fr", "de", "hi", "ru", "pt", "ja", "it", "nl"
}


def _configured_languages(workflow_configurations):
    value = (workflow_configurations or {}).get("language")
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (tuple, list)):
        values = value
    else:
        return []

    languages = []
    for item in values:
        label = str(item).strip()
        if not label:
            continue
        code = _LANGUAGE_LABELS.get(label.casefold(), label)
        if not code or not code[0].isalpha() or len(code.split("-")[0]) not in (2, 3):
            continue
        languages.append(code)
    return list(dict.fromkeys(languages))


def _provider_value(section):
    provider = getattr(section, "provider", None)
    return getattr(provider, "value", provider)


def apply_agent_language_configuration(config, workflow_configurations):
    """Apply the published agent language to provider configs without changing unset agents."""
    languages = _configured_languages(workflow_configurations)
    if not languages:
        return config

    effective = config.model_copy(deep=True)
    multi = bool((workflow_configurations or {}).get("multilingual_mode")) or len(languages) > 1
    primary = languages[0]
    primary_base = primary.split("-")[0].lower()
    transcription_language = None if multi else primary_base

    if effective.realtime is not None:
        realtime_updates = {"language": transcription_language}
        fields = getattr(type(effective.realtime), "model_fields", {})
        updates = {key: value for key, value in realtime_updates.items() if key in fields}
        if updates:
            effective.realtime = effective.realtime.model_copy(update=updates)

    if effective.stt is not None:
        provider = _provider_value(effective.stt)
        model = str(getattr(effective.stt, "model", ""))
        stt_updates = {}
        if provider == "deepgram":
            selected_bases = {code.split("-")[0].lower() for code in languages}
            if model in {"flux-general-en", "flux-general-multi"} and (
                primary_base != "en" or multi
            ):
                # Prefer Flux Multilingual where possible so the agent keeps
                # Flux turn detection. Its model supports ten languages; use
                # Nova-3 for the rest of Nova-3's broader language set.
                model = (
                    "flux-general-multi"
                    if selected_bases.issubset(_DEEPGRAM_FLUX_MULTILINGUAL_LANGUAGES)
                    else "nova-3-general"
                )
                stt_updates["model"] = model
            elif model == "flux-general-multi" and not selected_bases.issubset(
                _DEEPGRAM_FLUX_MULTILINGUAL_LANGUAGES
            ):
                model = "nova-3-general"
                stt_updates["model"] = model
            if model.startswith("nova-2") and multi:
                # Nova-2's multi mode only covers a narrower pair. Nova-3
                # handles the configured multilingual set used by the editor.
                model = "nova-3-general"
                stt_updates["model"] = model
            if model.startswith("nova-3"):
                stt_updates["language"] = "multi" if multi else primary_base
            elif model not in {"flux-general-en", "flux-general-multi"} and not multi:
                fields = getattr(type(effective.stt), "model_fields", {})
                if "language" in fields:
                    stt_updates["language"] = primary_base
        elif not multi and "language" in getattr(type(effective.stt), "model_fields", {}):
            stt_updates["language"] = primary
        if stt_updates:
            effective.stt = effective.stt.model_copy(update=stt_updates)

    if effective.tts is not None and not multi:
        fields = getattr(type(effective.tts), "model_fields", {})
        if "language" in fields:
            effective.tts = effective.tts.model_copy(update={"language": primary})

    return effective


def build_agent_language_instruction(workflow_configurations):
    """Return a short, agent-wide language rule for the published definition."""
    languages = _configured_languages(workflow_configurations)
    if not languages:
        return None
    labels = [
        f"{_LANGUAGE_NAMES.get(code.split('-')[0].lower(), code)} ({code})"
        for code in languages
    ]
    multi = bool((workflow_configurations or {}).get("multilingual_mode")) or len(languages) > 1
    if multi:
        return (
            "Language setting: support " + ", ".join(labels) + ". Detect which of these languages "
            "the caller is using and reply in that language. If the caller switches between the "
            "configured languages, follow their language while keeping the same agent role, facts, "
            "policies, and call flow."
        )
    return (
        f"Language setting: conduct the conversation in {labels[0]}. Understand caller speech in "
        "that language and speak all responses, greetings, and confirmations in it. Do not switch "
        "languages unless the caller explicitly asks you to."
    )


async def resolve_pipeline_voice(
    db, config, model_overrides, organization_id, user_id, *, strict_scope=False
):
    """Resolve the voice UUID with the existing live-call fallback rules."""
    if config.tts is None:
        return config
    voice_uuid = (model_overrides or {}).get("tts", {}).get("voice_uuid")
    if not voice_uuid:
        return config
    from api.services.configuration.org_provider_resolver import resolve_voice_for_tts

    if not organization_id:
        user = await db.get_user_by_id(user_id)
        if user:
            organization_id = user.selected_organization_id
    if strict_scope:
        row = await db.get_voice_for_private_planning(voice_uuid, organization_id)
        if row is None or not (
            row.organization_id == organization_id
            or row.is_public is True
            and row.status == "ready"
        ):
            raise ValueError("voice_reference_unavailable")
        if row.status != "ready":
            raise ValueError("voice_reference_not_ready")
        provider = getattr(config.tts.provider, "value", config.tts.provider)
        if (
            "elevenlabs" if row.provider == "dograh_clone" else row.provider
        ) != provider:
            raise ValueError("voice_provider_mismatch")
        voice = row.provider_voice_id
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("voice_reference_unavailable")
    else:
        voice = await resolve_voice_for_tts(voice_uuid, organization_id)
    if not voice:
        return config
    effective = config.model_copy(deep=True)
    effective.tts = effective.tts.model_copy(update={"voice": voice})
    return effective
