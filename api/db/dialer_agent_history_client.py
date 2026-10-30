"""Account-scoped Sysevo call bindings, separate from workflow storage."""
import httpx

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


class HistoryStorageUnavailable(RuntimeError):
    pass


async def rpc(name: str, payload: dict, *, authorization: str | None = None):
    key = SUPABASE_ANON_KEY if authorization else SUPABASE_SERVICE_ROLE_KEY
    if not SUPABASE_URL or not key:
        raise HistoryStorageUnavailable("Agent history storage is unavailable")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            result = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}",json=payload,
                headers={"apikey":key,"Authorization":authorization or f"Bearer {key}"})
            result.raise_for_status()
            return result.json()
    except Exception as exc:
        raise HistoryStorageUnavailable("Could not access agent call history") from exc
