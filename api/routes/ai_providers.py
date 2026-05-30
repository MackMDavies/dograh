"""API routes for org-level AI provider connection management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from api.db import db_client
from api.schemas.ai_providers import (
    AvailableModelResponseSchema,
    CreateProviderConnectionSchema,
    ProviderConnectionResponseSchema,
    SetModelClientAvailableSchema,
    UpdateProviderConnectionSchema,
)
from api.services.auth.depends import get_user

router = APIRouter(prefix="/ai-providers", tags=["ai-providers"])


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
    )


# ─── Admin: Connections ───────────────────────────────────────────────────────

@router.get("/connections", response_model=list[ProviderConnectionResponseSchema])
async def list_connections(user=Depends(get_user)):
    """List all active provider connections for the org. Admin only."""
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
    """List all models across all connections. Admins see everything; used for management UI."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    models = await db_client.list_available_models(
        user.selected_organization_id,
        service_type=service_type,
    )
    return [_model_to_schema(m, provider_by_conn.get(m.connection_id, "")) for m in models]


@router.get("/client-models", response_model=list[AvailableModelResponseSchema])
async def list_client_models(
    service_type: Optional[str] = None,
    user=Depends(get_user),
):
    """List only models marked as client-available. Used by client model selector."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    models = await db_client.list_available_models(
        user.selected_organization_id,
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
