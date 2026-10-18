"""Recover overdue parallel offers after an API worker crash/restart."""
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.providers.twilio.dialer_overflow_recovery import recover_overflow_offers


async def reconcile_dialer_overflow(_ctx) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        await recover_overflow_offers()
    except Exception as exc:
        logger.warning(f"Parallel overflow reconciliation deferred: {type(exc).__name__}")
