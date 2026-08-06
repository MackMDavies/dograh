"""Fish Audio voice catalog and public-library fetch service."""

from typing import Optional

import httpx
from loguru import logger
from sqlalchemy import select

from api.db import db_client
from api.db.models import UserModel

FISH_BASE_URL = "https://api.fish.audio"


async def get_system_fish_api_key() -> Optional[str]:
    """Return the platform Fish Audio key used for clients who don't bring their own.

    Mirrors get_system_elevenlabs_api_key(): checks a superuser's TTS
    user-configuration first, then any active Fish org provider connection
    (the key saved via AI Models -> Fish Audio).
    """
    async with db_client.async_session() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.is_superuser == True).limit(1)
        )
        superuser = result.scalars().first()
    if superuser:
        config = await db_client.get_user_configurations(superuser.id)
        if config.tts:
            tts = config.tts.model_dump()
            if tts.get("provider") == "fish":
                api_key = tts.get("api_key")
                if isinstance(api_key, list):
                    api_key = api_key[0] if api_key else None
                if api_key:
                    return api_key

    try:
        all_conns = await db_client.list_all_connections_superuser(service_type="tts")
        conn = next((c for c in all_conns if c.provider == "fish" and c.api_key), None)
        if conn and conn.api_key:
            return conn.api_key
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Could not read org provider connections for system Fish key: {e}")

    logger.warning("No system Fish Audio API key found (user-config or org connection)")
    return None


async def get_caller_fish_api_key(user_id: int) -> Optional[str]:
    """Get the Fish Audio API key from the calling user's own TTS configuration."""
    config = await db_client.get_user_configurations(user_id)
    if not config.tts:
        return None
    tts = config.tts.model_dump()
    if tts.get("provider") != "fish":
        return None
    key = tts.get("api_key")
    if isinstance(key, list):
        return key[0] if key else None
    return key


async def fetch_fish_catalog(api_key: str) -> list[dict]:
    """Fetch voices owned by the connected Fish Audio account."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(
            f"{FISH_BASE_URL}/model",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"self": "true", "page_size": 100},
        )
        response.raise_for_status()
        return response.json().get("items", [])


async def fetch_fish_public_voices(
    api_key: str,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    language: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
) -> dict:
    """Search Fish Audio's public voice library.

    Returns the raw paginated response: {"items": [...], "total": int,
    "has_more": bool}.
    """
    params: dict = {"page_number": page, "page_size": page_size}
    if search:
        params["title"] = search
    if tag:
        params["tag"] = tag
    if language:
        params["language"] = language

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(
            f"{FISH_BASE_URL}/model",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
        response.raise_for_status()
        return response.json()


async def fetch_fish_voice_by_id(api_key: str, voice_id: str) -> Optional[dict]:
    """Fetch a single Fish Audio voice model by its id.

    Returns None when the voice can't be fetched, so callers can fall back
    rather than failing the whole import.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            response = await client.get(
                f"{FISH_BASE_URL}/model/{voice_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.warning(f"Could not fetch Fish Audio voice {voice_id}: {e}")
        return None
