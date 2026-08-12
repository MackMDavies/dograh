"""Supabase writes for dialer_call_listeners - tracks who's currently
listening in on a live dialer call.

Same reasoning as dialer_call_log.py: this table lives entirely in
Supabase, so every write is a server-to-server REST call using the
service-role key. Fail-soft (never raises) for the same reason as
dialer_call_log.py's functions - this runs inside a Twilio webhook that
must always return valid TwiML regardless of whether this write succeeds.
"""

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

_LISTENERS_URL_SUFFIX = "/rest/v1/dialer_call_listeners"


def _headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def create_dialer_call_listener(*, parent_call_sid: str, manager_user_id: str) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot create dialer_call_listeners row")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_LISTENERS_URL_SUFFIX}",
                json={"parent_call_sid": parent_call_sid, "manager_user_id": manager_user_id},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to create dialer_call_listeners row for {parent_call_sid}: {exc}")
