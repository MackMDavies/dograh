"""ElevenLabs voice cloning and catalog service for the voice library."""

from typing import Optional

import httpx
from loguru import logger
from sqlalchemy import select

from api.db import db_client
from api.db.models import UserModel

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"


async def get_system_elevenlabs_api_key() -> Optional[str]:
    """Return the platform ElevenLabs key used for clients who don't bring their own.

    Looks in two places, in order:
      1) a superuser's TTS *user-configuration* (config.tts), and
      2) any active ElevenLabs *org provider connection* — this is where the
         admin's "AI Models -> ElevenLabs" key is actually saved.
    Previously only (1) was checked, so a key added via AI Models (which writes
    the connection, not the user-config) was invisible and clients got
    'No ElevenLabs API key found'.
    """
    # 1) superuser user-configuration (legacy location)
    async with db_client.async_session() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.is_superuser == True).limit(1)
        )
        superuser = result.scalars().first()
    if superuser:
        config = await db_client.get_user_configurations(superuser.id)
        if config.tts:
            tts = config.tts.model_dump()
            if tts.get("provider") == "elevenlabs":
                api_key = tts.get("api_key")
                if isinstance(api_key, list):
                    api_key = api_key[0] if api_key else None
                if api_key:
                    return api_key

    # 2) any active ElevenLabs org provider connection (AI Models -> ElevenLabs)
    try:
        all_conns = await db_client.list_all_connections_superuser(service_type="tts")
        conn = next((c for c in all_conns if c.provider == "elevenlabs" and c.api_key), None)
        if conn and conn.api_key:
            return conn.api_key
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Could not read org provider connections for system EL key: {e}")

    logger.warning("No system ElevenLabs API key found (user-config or org connection)")
    return None


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


async def fetch_elevenlabs_shared_voices(
    api_key: str,
    page_size: int = 30,
    page: int = 1,
    search: Optional[str] = None,
    language: Optional[str] = None,
    gender: Optional[str] = None,
    age: Optional[str] = None,
    use_case: Optional[str] = None,
    accent: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Search the ElevenLabs public shared voice library.

    Returns a dict with 'voices' list and 'has_more' / 'total_count' fields.
    Shared voices can be used directly in TTS by voice_id without adding to account.
    """
    params: dict = {"page_size": page_size, "page": page}
    if search:
        params["search"] = search
    if language:
        params["language"] = language
    if gender:
        params["gender"] = gender
    if age:
        params["age"] = age
    if use_case:
        params["use_case"] = use_case
    if accent:
        params["accent"] = accent
    if category:
        params["category"] = category

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(
            f"{ELEVENLABS_BASE_URL}/v1/shared-voices",
            headers={"xi-api-key": api_key},
            params=params,
        )
        response.raise_for_status()
        return response.json()
