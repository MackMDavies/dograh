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
