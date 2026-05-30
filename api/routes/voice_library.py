"""API routes for the voice library."""

import os
import tempfile
import uuid as uuid_lib
from typing import Optional

import aiohttp
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from loguru import logger

from api.db import db_client
from api.db.models import UserModel
from api.schemas.voice_library import (
    ElevenLabsCatalogVoiceSchema,
    ElevenLabsImportRequestSchema,
    VoiceLibraryResponseSchema,
    VoiceLibraryUpdateSchema,
)
from api.services.auth.depends import get_user
from api.services.storage import storage_fs
from api.services.voice_library.elevenlabs_service import (
    clone_voice_with_elevenlabs,
    fetch_elevenlabs_catalog,
    get_caller_elevenlabs_api_key,
    get_system_elevenlabs_api_key,
)

_XAI_TTS_URL = "https://api.x.ai/v1/tts"

router = APIRouter(prefix="/voice-library", tags=["voice-library"])


def _serialize(voice) -> VoiceLibraryResponseSchema:
    return VoiceLibraryResponseSchema.model_validate(voice)


@router.get("", response_model=list[VoiceLibraryResponseSchema])
async def list_voices(
    language: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    age: Optional[str] = Query(None),
    use_case: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    user: UserModel = Depends(get_user),
) -> list[VoiceLibraryResponseSchema]:
    voices = await db_client.list_voices(
        organization_id=user.selected_organization_id,
        user_id=user.id,
        is_superuser=user.is_superuser,
        language=language,
        gender=gender,
        age=age,
        use_case=use_case,
        provider=provider,
        status=status,
    )
    return [_serialize(v) for v in voices]


_PROVIDER_PREDEFINED_VOICES: dict[str, list[dict]] = {
    "xai": [
        {"name": "Eve",  "voice_id": "eve",  "gender": "female"},
        {"name": "Ara",  "voice_id": "ara",  "gender": "female"},
        {"name": "Rex",  "voice_id": "rex",  "gender": "male"},
        {"name": "Sal",  "voice_id": "sal",  "gender": "male"},
        {"name": "Leo",  "voice_id": "leo",  "gender": "male"},
    ],
}


async def _fetch_xai_voices(api_key: str) -> list[dict]:
    """Fetch all available voices from xAI TTS API (built-in + custom)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.x.ai/v1/tts/voices",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("voices", [])
    except Exception as e:
        logger.warning(f"Failed to fetch xAI voices: {e}")
        return []


@router.post("/sync-providers")
async def sync_provider_voices(user: UserModel = Depends(get_user)):
    """Seed predefined voices for all active TTS provider connections that have known voice lists."""

    org_id = user.selected_organization_id
    conns = await db_client.list_connections(organization_id=org_id, service_type="tts")
    created = 0

    for conn in conns:
        if conn.provider == "xai" and conn.api_key:
            # Try to fetch voices dynamically from xAI API
            api_voices = await _fetch_xai_voices(conn.api_key)
            if api_voices:
                for av in api_voices:
                    vid = av.get("voice_id") or av.get("id", "")
                    if not vid:
                        continue
                    existing = await db_client.get_voice_by_provider_id(vid, org_id)
                    if existing:
                        continue
                    await db_client.create_voice(
                        user_id=user.id,
                        organization_id=org_id,
                        name=av.get("name", vid.capitalize()),
                        provider="xai",
                        provider_voice_id=vid,
                        gender=av.get("gender"),
                        is_public=True,
                        status="ready",
                        language=av.get("language") or "en",
                        labels={"provider": "xai"},
                    )
                    created += 1
                    logger.info(f"Synced xAI voice '{vid}' for org {org_id}")
                continue  # skip hardcoded fallback if API succeeded

        # Hardcoded fallback for all providers
        voice_defs = _PROVIDER_PREDEFINED_VOICES.get(conn.provider, [])
        for vdef in voice_defs:
            existing = await db_client.get_voice_by_provider_id(vdef["voice_id"], org_id)
            if existing:
                continue
            await db_client.create_voice(
                user_id=user.id,
                organization_id=org_id,
                name=vdef["name"],
                provider=conn.provider,
                provider_voice_id=vdef["voice_id"],
                gender=vdef.get("gender"),
                is_public=True,
                status="ready",
                language="en",
                labels={"provider": conn.provider},
            )
            created += 1
            logger.info(f"Synced voice '{vdef['voice_id']}' ({conn.provider}) for org {org_id}")

    return {"synced": created}


@router.get("/elevenlabs/voices", response_model=list[ElevenLabsCatalogVoiceSchema])
async def get_elevenlabs_catalog(user: UserModel = Depends(get_user)) -> list[ElevenLabsCatalogVoiceSchema]:
    api_key = await get_caller_elevenlabs_api_key(user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="ElevenLabs API key not configured in your Voice Models settings")
    try:
        voices = await fetch_elevenlabs_catalog(api_key)
    except Exception as e:
        logger.error(f"EL catalog fetch failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch ElevenLabs catalog")
    return [
        ElevenLabsCatalogVoiceSchema(
            voice_id=v.get("voice_id", ""),
            name=v.get("name", ""),
            preview_url=v.get("preview_url"),
            labels=v.get("labels", {}),
            category=v.get("category"),
        )
        for v in voices
    ]


@router.post("/import/elevenlabs", response_model=list[VoiceLibraryResponseSchema], status_code=201)
async def import_elevenlabs_voices(
    body: ElevenLabsImportRequestSchema,
    user: UserModel = Depends(get_user),
) -> list[VoiceLibraryResponseSchema]:
    api_key = await get_caller_elevenlabs_api_key(user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="ElevenLabs API key not configured")
    try:
        catalog = await fetch_elevenlabs_catalog(api_key)
    except Exception as e:
        logger.error(f"EL catalog fetch failed during import: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch ElevenLabs catalog")

    catalog_map = {v["voice_id"]: v for v in catalog}
    created = []
    for voice_id in body.voice_ids:
        el_voice = catalog_map.get(voice_id)
        if not el_voice:
            continue
        existing = await db_client.get_voice_by_provider_id(voice_id, user.selected_organization_id)
        if existing:
            logger.info(f"EL voice {voice_id} already in library, skipping")
            continue
        labels = el_voice.get("labels", {})
        voice = await db_client.create_voice(
            user_id=user.id,
            organization_id=user.selected_organization_id,
            name=el_voice.get("name", voice_id),
            provider="elevenlabs",
            provider_voice_id=voice_id,
            is_public=body.is_public,
            language=labels.get("language"),
            accent=labels.get("accent"),
            gender=labels.get("gender"),
            age=labels.get("age"),
            use_case=labels.get("use_case"),
            audio_preview_url=el_voice.get("preview_url"),
            labels=labels,
            status="ready",
        )
        created.append(_serialize(voice))
    return created


@router.post("/clone", response_model=VoiceLibraryResponseSchema, status_code=201)
async def clone_voice(
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    description: str = Form(""),
    is_public: bool = Form(False),
    language: Optional[str] = Form(None),
    accent: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    age: Optional[str] = Form(None),
    use_case: Optional[str] = Form(None),
    tts_provider: str = Form("elevenlabs"),
    file: UploadFile = File(...),
    user: UserModel = Depends(get_user),
) -> VoiceLibraryResponseSchema:
    audio_data = await file.read()
    if len(audio_data) < 1000:
        raise HTTPException(status_code=400, detail="Audio file too small — minimum 1 second of audio required")

    provider = "xai" if tts_provider == "xai" else "dograh_clone"
    voice = await db_client.create_voice(
        user_id=user.id,
        organization_id=user.selected_organization_id,
        name=name,
        description=description or None,
        provider=provider,
        is_public=is_public,
        language=language or None,
        accent=accent or None,
        gender=gender or None,
        age=age or None,
        use_case=use_case or None,
        status="pending",
    )
    content_type = file.content_type or "audio/webm"
    filename = file.filename or "recording.webm"
    if tts_provider == "xai":
        background_tasks.add_task(
            _process_xai_clone_background,
            voice.uuid,
            name,
            description,
            audio_data,
            filename,
            content_type,
            user.selected_organization_id,
        )
    else:
        background_tasks.add_task(
            _process_clone_background,
            voice.uuid,
            name,
            description,
            audio_data,
            filename,
            content_type,
        )
    return _serialize(voice)


async def _process_clone_background(
    voice_uuid: str,
    name: str,
    description: str,
    audio_data: bytes,
    filename: str,
    content_type: str,
) -> None:
    api_key = await get_system_elevenlabs_api_key()
    if not api_key:
        logger.error(f"No system EL API key — cannot clone voice {voice_uuid}")
        await db_client.update_voice_status(voice_uuid, "failed")
        return
    try:
        await db_client.update_voice_status(voice_uuid, "processing")
        result = await clone_voice_with_elevenlabs(api_key, name, audio_data, description, filename, content_type)
        el_voice_id = result.get("voice_id")
        await db_client.update_voice_status(voice_uuid, "ready", provider_voice_id=el_voice_id)
        logger.info(f"Voice clone {voice_uuid} ready — EL voice_id: {el_voice_id}")
    except Exception as e:
        logger.error(f"Voice clone {voice_uuid} failed: {e}")
        await db_client.update_voice_status(voice_uuid, "failed")


async def _process_xai_clone_background(
    voice_uuid: str,
    name: str,
    description: str,
    audio_data: bytes,
    filename: str,
    content_type: str,
    org_id: int,
) -> None:
    conn = await db_client.get_connection_by_provider(org_id, "tts", "xai")
    if not conn or not conn.api_key:
        logger.error(f"No xAI TTS connection for org {org_id} — cannot clone voice {voice_uuid}")
        await db_client.update_voice_status(voice_uuid, "failed")
        return
    try:
        await db_client.update_voice_status(voice_uuid, "processing")
        form = aiohttp.FormData()
        form.add_field("file", audio_data, filename=filename, content_type=content_type)
        form.add_field("name", name)
        if description:
            form.add_field("description", description)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.x.ai/v1/custom-voices",
                headers={"Authorization": f"Bearer {conn.api_key}"},
                data=form,
            ) as resp:
                if resp.status != 201:
                    error_text = await resp.text()
                    logger.error(f"xAI custom voice creation failed {resp.status}: {error_text}")
                    await db_client.update_voice_status(voice_uuid, "failed")
                    return
                result = await resp.json()
        xai_voice_id = result.get("voice_id")
        await db_client.update_voice_status(voice_uuid, "ready", provider_voice_id=xai_voice_id)
        logger.info(f"xAI voice clone {voice_uuid} ready — voice_id: {xai_voice_id}")
    except Exception as e:
        logger.error(f"xAI voice clone {voice_uuid} failed: {e}")
        await db_client.update_voice_status(voice_uuid, "failed")


@router.post("/{voice_uuid}/generate-preview", response_model=VoiceLibraryResponseSchema)
async def generate_voice_preview(
    voice_uuid: str,
    user: UserModel = Depends(get_user),
) -> VoiceLibraryResponseSchema:
    """Generate an audio preview for a voice and store it in object storage."""
    org_id = user.selected_organization_id

    # 1. Fetch the voice entry (org-scoped)
    voice = await db_client.get_voice_by_uuid(voice_uuid, org_id)
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found")

    # 2. Only xAI voices support HTTP preview generation
    if voice.provider != "xai":
        raise HTTPException(
            status_code=422,
            detail=f"Preview generation is not supported for provider '{voice.provider}'",
        )

    # 3. Get the org's xAI TTS connection to retrieve the API key
    conn = await db_client.get_connection_by_provider(org_id, "tts", voice.provider)
    if not conn or not conn.api_key:
        raise HTTPException(
            status_code=400,
            detail="No active xAI TTS connection found for this organisation",
        )

    # 4. Call xAI TTS API to generate the preview
    sample_text = f"Hello, I'm {voice.name}. How can I help you today?"
    payload = {
        "text": sample_text,
        "voice_id": voice.provider_voice_id,
        "language": "en",
        "output_format": {"codec": "mp3"},
    }
    headers = {
        "Authorization": f"Bearer {conn.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_XAI_TTS_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"xAI TTS API error {resp.status}: {error_text}")
                    raise HTTPException(
                        status_code=502,
                        detail=f"xAI TTS API returned {resp.status}",
                    )
                audio_bytes = await resp.read()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error calling xAI TTS API for voice {voice_uuid}: {exc}")
        raise HTTPException(status_code=502, detail="Failed to call xAI TTS API") from exc

    # 5. Upload audio to object storage via a temp file
    storage_key = f"voice-previews/{voice_uuid}/{uuid_lib.uuid4()}.mp3"
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="dograh_preview_")
        os.close(fd)
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)

        upload_ok = await storage_fs.aupload_file(tmp_path, storage_key)
        if not upload_ok:
            raise HTTPException(status_code=500, detail="Failed to upload preview audio to storage")
        preview_url = await storage_fs.aget_signed_url(storage_key)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error storing preview audio for voice {voice_uuid}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to store preview audio") from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # 6. Update the voice record with the new preview URL
    await db_client.update_voice_status(
        voice_uuid=voice_uuid,
        status=voice.status,
        audio_preview_url=preview_url,
    )

    # 7. Return the updated voice
    updated_voice = await db_client.get_voice_by_uuid(voice_uuid, org_id)
    return _serialize(updated_voice)


@router.get("/{voice_uuid}", response_model=VoiceLibraryResponseSchema)
async def get_voice(
    voice_uuid: str,
    user: UserModel = Depends(get_user),
) -> VoiceLibraryResponseSchema:
    voice = await db_client.get_voice_by_uuid(voice_uuid, user.selected_organization_id)
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found")
    if not voice.is_public and voice.user_id != user.id and not user.is_superuser:
        raise HTTPException(status_code=403, detail="Access denied")
    return _serialize(voice)


@router.put("/{voice_uuid}", response_model=VoiceLibraryResponseSchema)
async def update_voice(
    voice_uuid: str,
    body: VoiceLibraryUpdateSchema,
    user: UserModel = Depends(get_user),
) -> VoiceLibraryResponseSchema:
    voice = await db_client.update_voice(
        voice_uuid=voice_uuid,
        organization_id=user.selected_organization_id,
        user_id=user.id,
        is_superuser=user.is_superuser,
        **body.model_dump(exclude_none=True),
    )
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found or access denied")
    return _serialize(voice)


@router.delete("/{voice_uuid}", status_code=204)
async def delete_voice(
    voice_uuid: str,
    user: UserModel = Depends(get_user),
) -> None:
    deleted = await db_client.delete_voice(
        voice_uuid=voice_uuid,
        organization_id=user.selected_organization_id,
        user_id=user.id,
        is_superuser=user.is_superuser,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice not found or access denied")
