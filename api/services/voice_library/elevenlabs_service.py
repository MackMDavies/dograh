"""ElevenLabs voice cloning and catalog service for the voice library."""

from typing import Optional

import httpx
from loguru import logger
from sqlalchemy import select

from api.db import db_client
from api.db.models import UserModel

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"


async def get_system_elevenlabs_api_key() -> Optional[str]:
    """Return the ElevenLabs API key from the first superuser's TTS configuration."""
    async with db_client.async_session() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.is_superuser == True).limit(1)
        )
        superuser = result.scalars().first()
    if not superuser:
        logger.warning("No superuser found — cannot retrieve system EL API key")
        return None

    config = await db_client.get_user_configurations(superuser.id)
    if not config.tts:
        return None
    tts = config.tts.model_dump()
    if tts.get("provider") != "elevenlabs":
        return None
    api_key = tts.get("api_key")
    if not api_key:
        return None
    if isinstance(api_key, list):
        return api_key[0] if api_key else None
    return api_key


async def get_caller_elevenlabs_api_key(user_id: int) -> Optional[str]:
    """Get the ElevenLabs API key from the calling user's own TTS configuration.
    Used for admin-initiated catalog operations where the caller's key is appropriate.
    """
    config = await db_client.get_user_configurations(user_id)
    if not config.tts:
        return None
    tts = config.tts.model_dump()
    if tts.get("provider") != "elevenlabs":
        return None
    key = tts.get("api_key")
    if isinstance(key, list):
        return key[0] if key else None
    return key


async def clone_voice_with_elevenlabs(
    api_key: str,
    name: str,
    audio_data: bytes,
    description: str = "",
    filename: str = "recording.webm",
    content_type: str = "audio/webm",
) -> dict:
    """Submit audio to ElevenLabs Instant Voice Clone API. Returns {voice_id: str}."""
    # ElevenLabs rejects MIME types with codec suffixes like "audio/webm;codecs=opus"
    if ";" in content_type:
        content_type = content_type.split(";")[0].strip()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        response = await client.post(
            f"{ELEVENLABS_BASE_URL}/v1/voices/add",
            headers={"xi-api-key": api_key},
            files={"files": (filename, audio_data, content_type)},
            data={"name": name, "description": description},
        )
        if not response.is_success:
            logger.error(
                f"ElevenLabs clone rejected: HTTP {response.status_code} — {response.text}"
            )
        response.raise_for_status()
        return response.json()


async def fetch_elevenlabs_catalog(api_key: str) -> list[dict]:
    """Fetch all voices from an ElevenLabs account."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(
            f"{ELEVENLABS_BASE_URL}/v1/voices",
            headers={"xi-api-key": api_key},
        )
        response.raise_for_status()
        return response.json().get("voices", [])
