"""Resolve the account that owns a signed inbound call or authenticated user."""
import httpx

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


async def inbound_scope(*, number=None, user_id=None, call_id=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/dialer_inbound_agent_scope",
            json={"p_number":number,"p_user_id":user_id,"p_call_id":call_id},
            headers={"apikey":SUPABASE_SERVICE_ROLE_KEY,
                "Authorization":f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
        )
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value,dict) else None
