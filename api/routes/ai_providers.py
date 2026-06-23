"""API routes for org-level AI provider connection management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from api.db import db_client
from api.schemas.ai_providers import (
    ApplyMarginSchema,
    AvailableModelResponseSchema,
    CreateProviderConnectionSchema,
    ProviderConnectionResponseSchema,
    SetModelClientAvailableSchema,
    SetModelDisplayNameSchema,
    SetModelOurPriceSchema,
    UpdateProviderConnectionSchema,
)
from api.services.auth.depends import get_user
from api.services.configuration.pricing import get_model_pricing

router = APIRouter(prefix="/ai-providers", tags=["ai-providers"])

# ─── Pricing catalog ──────────────────────────────────────────────────────────
# Moved to api.services.configuration.pricing — imported above as get_model_pricing

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mask_key(key: Optional[str]) -> Optional[str]:
    if not key or len(key) < 4:
        return key
    return "****" + key[-4:]


def _conn_to_schema(conn, model_count: int = 0) -> ProviderConnectionResponseSchema:
    return ProviderConnectionResponseSchema(
        id=conn.id,
        organization_id=conn.organization_id,
        service_type=conn.service_type,
        provider=conn.provider,
        display_name=conn.display_name,
        api_key_masked=_mask_key(conn.api_key),
        extra_config=conn.extra_config,
        is_active=conn.is_active,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
        model_count=model_count,
    )


def _model_to_schema(m, provider: str) -> AvailableModelResponseSchema:
    return AvailableModelResponseSchema(
        id=m.id,
        connection_id=m.connection_id,
        organization_id=m.organization_id,
        service_type=m.service_type,
        provider=provider,
        model_id=m.model_id,
        display_name=m.display_name,
        is_client_available=m.is_client_available,
        is_default=m.is_default,
        cost_per_min_usd=m.cost_per_min_usd,
        native_cost_display=m.native_cost_display,
        our_price_per_min_usd=m.our_price_per_min_usd,
    )


# ─── Admin: Connections ───────────────────────────────────────────────────────

@router.get("/connections", response_model=list[ProviderConnectionResponseSchema])
async def list_connections(user=Depends(get_user)):
    """List active provider connections. Superusers see all orgs; others see their own."""
    if user.is_superuser:
        conns = await db_client.list_all_connections_superuser()
        all_models = await db_client.list_all_available_models_superuser()
    else:
        conns = await db_client.list_connections(user.selected_organization_id)
        all_models = await db_client.list_available_models(user.selected_organization_id)
    count_by_conn: dict[int, int] = {}
    for m in all_models:
        count_by_conn[m.connection_id] = count_by_conn.get(m.connection_id, 0) + 1
    return [_conn_to_schema(c, count_by_conn.get(c.id, 0)) for c in conns]


_XAI_TTS_VOICES = ["eve", "ara", "rex", "sal", "leo"]


@router.post("/connections", response_model=ProviderConnectionResponseSchema)
async def create_connection(
    body: CreateProviderConnectionSchema,
    user=Depends(get_user),
):
    """Connect a new API provider to the org. Admin only."""
    try:
        conn = await db_client.create_connection(
            organization_id=user.selected_organization_id,
            created_by=user.id,
            service_type=body.service_type,
            provider=body.provider,
            api_key=body.api_key,
            extra_config=body.extra_config,
            display_name=body.display_name,
        )

        # Seed predefined xAI TTS voices into the voice library
        if body.service_type == "tts" and body.provider == "xai":
            org_id = user.selected_organization_id
            for voice_name in _XAI_TTS_VOICES:
                existing = await db_client.get_voice_by_provider_id(voice_name, org_id)
                if existing:
                    logger.info(f"xAI voice '{voice_name}' already exists for org {org_id}, skipping")
                    continue
                await db_client.create_voice(
                    user_id=user.id,
                    organization_id=org_id,
                    name=voice_name.capitalize(),
                    provider="xai",
                    provider_voice_id=voice_name,
                    is_public=True,
                    status="ready",
                    language="en",
                    labels={"provider": "xai"},
                )
                logger.info(f"Seeded xAI voice '{voice_name}' for org {org_id}")

        return _conn_to_schema(conn)
    except Exception as exc:
        logger.error(f"Error creating provider connection: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create connection") from exc


@router.post("/connections/{connection_id}/sync-voices")
async def sync_connection_voices(connection_id: int, user=Depends(get_user)):
    """Seed predefined voices into the voice library for an existing TTS connection."""
    conn = await db_client.get_connection(connection_id, user.selected_organization_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if conn.service_type != "tts":
        raise HTTPException(status_code=422, detail="Only TTS connections have predefined voices")

    provider_voices: dict[str, list[str]] = {
        "xai": _XAI_TTS_VOICES,
    }
    voice_names = provider_voices.get(conn.provider, [])
    if not voice_names:
        raise HTTPException(status_code=422, detail=f"No predefined voices for provider '{conn.provider}'")

    created = 0
    org_id = user.selected_organization_id
    for voice_name in voice_names:
        existing = await db_client.get_voice_by_provider_id(voice_name, org_id)
        if existing:
            continue
        await db_client.create_voice(
            user_id=user.id,
            organization_id=org_id,
            name=voice_name.capitalize(),
            provider=conn.provider,
            provider_voice_id=voice_name,
            is_public=True,
            status="ready",
            language="en",
            labels={"provider": conn.provider},
        )
        created += 1
        logger.info(f"Seeded voice '{voice_name}' ({conn.provider}) for org {org_id}")

    return {"created": created, "total": len(voice_names)}


@router.post("/connections/{connection_id}/sync-models")
async def sync_connection_models(connection_id: int, user=Depends(get_user)):
    """Re-fetch and reseed models for a connection from the live provider API (falls back to catalog)."""
    count = await db_client.reseed_models_for_connection(
        connection_id=connection_id,
        organization_id=user.selected_organization_id,
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="Connection not found or no models available")
    return {"connection_id": connection_id, "model_count": count}


@router.put("/connections/{connection_id}", response_model=ProviderConnectionResponseSchema)
async def update_connection(
    connection_id: int,
    body: UpdateProviderConnectionSchema,
    user=Depends(get_user),
):
    """Update API key or config for an existing connection. Admin only."""
    conn = await db_client.update_connection(
        connection_id=connection_id,
        organization_id=user.selected_organization_id,
        api_key=body.api_key,
        extra_config=body.extra_config,
        display_name=body.display_name,
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _conn_to_schema(conn)


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: int, user=Depends(get_user)):
    """Soft-delete a provider connection and all its models. Admin only."""
    removed = await db_client.delete_connection(
        connection_id=connection_id,
        organization_id=user.selected_organization_id,
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Connection not found")
    return {"success": True}


# ─── Admin + Client: Models ───────────────────────────────────────────────────

@router.get("/models", response_model=list[AvailableModelResponseSchema])
async def list_models(
    service_type: Optional[str] = None,
    user=Depends(get_user),
):
    """List all models. Superusers see all orgs; others see their own."""
    if user.is_superuser:
        conns = await db_client.list_all_connections_superuser()
        models = await db_client.list_all_available_models_superuser(service_type=service_type)
    else:
        conns = await db_client.list_connections(user.selected_organization_id)
        models = await db_client.list_available_models(
            user.selected_organization_id,
            service_type=service_type,
        )
    provider_by_conn = {c.id: c.provider for c in conns}
    return [_model_to_schema(m, provider_by_conn.get(m.connection_id, "")) for m in models]


@router.get("/client-models", response_model=list[AvailableModelResponseSchema])
async def list_client_models(
    service_type: Optional[str] = None,
    organization_id: Optional[int] = None,
    user=Depends(get_user),
):
    """List only models marked as client-available. Used by client model selector.

    Superusers see their own org's models (for admin preview / org override).
    Non-superusers (voice hub subscription clients) always see models from the
    platform admin's org — the superuser who configured the providers.
    """
    if user.is_superuser:
        effective_org_id = organization_id if organization_id is not None else user.selected_organization_id
    else:
        # All voice hub clients share the platform admin's AI providers/models.
        platform_org_id = await db_client.get_platform_organization_id()
        effective_org_id = platform_org_id if platform_org_id is not None else user.selected_organization_id

    conns = await db_client.list_connections(effective_org_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    models = await db_client.list_available_models(
        effective_org_id,
        service_type=service_type,
        client_only=True,
    )
    return [_model_to_schema(m, provider_by_conn.get(m.connection_id, "")) for m in models]


@router.patch("/models/{model_pk}/availability", response_model=AvailableModelResponseSchema)
async def set_model_availability(
    model_pk: int,
    body: SetModelClientAvailableSchema,
    user=Depends(get_user),
):
    """Toggle whether a model is visible to clients. Admin only."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    m = await db_client.set_model_client_available(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        is_client_available=body.is_client_available,
    )
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


@router.patch("/models/{model_pk}/default", response_model=AvailableModelResponseSchema)
async def set_model_default(
    model_pk: int,
    user=Depends(get_user),
):
    """Mark a model as the default for its service type (clears previous default). Admin only."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    all_models = await db_client.list_available_models(user.selected_organization_id)
    target = next((x for x in all_models if x.id == model_pk), None)
    if not target:
        raise HTTPException(status_code=404, detail="Model not found")

    m = await db_client.set_model_default(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        service_type=target.service_type,
    )
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


@router.patch("/models/{model_pk}/display-name", response_model=AvailableModelResponseSchema)
async def set_model_display_name(
    model_pk: int,
    body: SetModelDisplayNameSchema,
    user=Depends(get_user),
):
    """Set a human-readable display name for a model. Pass null to clear."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    m = await db_client.set_model_display_name(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        display_name=body.display_name,
    )
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


@router.patch("/models/{model_pk}/our-price", response_model=AvailableModelResponseSchema)
async def set_model_our_price(
    model_pk: int,
    body: SetModelOurPriceSchema,
    user=Depends(get_user),
):
    """Set the 'our price per minute' for a model. Admin only."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    m = await db_client.set_model_our_price(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        our_price_per_min_usd=body.our_price_per_min_usd,
    )
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


# ─── Pricing ──────────────────────────────────────────────────────────────────

@router.post("/pricing/refresh")
async def refresh_pricing(user=Depends(get_user)):
    """Populate cost_per_min_usd and native_cost_display for all models from the pricing catalog."""
    models = await db_client.list_available_models(user.selected_organization_id)
    upserted = 0
    for m in models:
        pricing = get_model_pricing(m.model_id)
        if pricing is None:
            continue
        cost_per_min, native_display = pricing
        await db_client.set_model_cost(
            model_id_pk=m.id,
            organization_id=user.selected_organization_id,
            cost_per_min_usd=cost_per_min,
            native_cost_display=native_display,
        )
        upserted += 1
    logger.info(f"Pricing refresh: populated {upserted}/{len(models)} models for org {user.selected_organization_id}")
    return {"upserted": upserted, "total": len(models)}


@router.post("/pricing/apply-margin")
async def apply_margin(body: ApplyMarginSchema, user=Depends(get_user)):
    """Calculate our_price_per_min = cost_per_min * (1 + margin/100) for all priced models."""
    models = await db_client.list_available_models(user.selected_organization_id)
    if body.connection_id is not None:
        models = [m for m in models if m.connection_id == body.connection_id]

    multiplier = 1.0 + (body.margin_percent / 100.0)
    updated = 0
    for m in models:
        if m.cost_per_min_usd is None:
            continue
        our_price = round(m.cost_per_min_usd * multiplier, 6)
        await db_client.set_model_our_price(
            model_id_pk=m.id,
            organization_id=user.selected_organization_id,
            our_price_per_min_usd=our_price,
        )
        updated += 1
    return {"updated": updated}
