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


async def resolve_pipeline_voice(db, config, model_overrides, organization_id, user_id):
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
    voice = await resolve_voice_for_tts(voice_uuid, organization_id)
    if not voice:
        return config
    effective = config.model_copy(deep=True)
    effective.tts = effective.tts.model_copy(update={"voice": voice})
    return effective
