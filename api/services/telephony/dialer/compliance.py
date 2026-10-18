"""Server-side compliance checks shared by dialer telephony providers."""

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def phone_block_reason(phone: str) -> str | None:
    """Return a suppression/terminal reason, or fail closed on uncertainty."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return "compliance_unavailable"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_service_phone_block_reason",
                json={"p_phone": phone},
                headers=_headers(),
            )
            response.raise_for_status()
            reason = response.json()
        return reason if isinstance(reason, str) and reason else None
    except Exception as exc:  # noqa: BLE001 - unknown compliance state blocks the dial
        logger.error(f"Could not verify dialer phone suppression: {exc}")
        return "compliance_unavailable"


async def calling_hours_result(
    phone: str, entry_id: str | None = None
) -> tuple[bool, str | None, str | None]:
    """Return the server compliance decision and its authoritative US state."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False, "compliance_unavailable", None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/functions/v1/dialer-calling-hours-check",
                json={"phone": phone, "entry_id": entry_id},
                headers=_headers(),
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or result.get("allowed") is not True:
            reason = result.get("reason") if isinstance(result, dict) else None
            state = result.get("us_state") if isinstance(result, dict) else None
            return (
                False,
                reason if isinstance(reason, str) else "compliance_unavailable",
                state if isinstance(state, str) else None,
            )
        state = result.get("us_state")
        return True, None, state if isinstance(state, str) else None
    except Exception as exc:  # noqa: BLE001 - never treat uncertainty as permission
        logger.error(f"Could not verify dialer calling hours: {exc}")
        return False, "compliance_unavailable", None


async def calling_hours_allowed(phone: str, entry_id: str | None = None) -> tuple[bool, str | None]:
    """Compatibility wrapper for callers that need only allow/block."""
    allowed, reason, _state = await calling_hours_result(phone, entry_id)
    return allowed, reason
