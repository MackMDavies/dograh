"""Dial-time suppression check.

Backs CampaignCallDispatcher.dispatch_call: before dialling, ask whether this
number is on the do-not-dial register. The register itself lives in Supabase
(`dial_suppression`, written from /voice/dispositions' Suppress action); this
module only ever talks to a Redis mirror kept fresh by the sync_dial_suppression
ARQ cron (api/tasks/dial_suppression_sync.py), falling back to a direct
Supabase check when Redis itself is unreachable.

No-ops (returns False, i.e. never suppressed) when SYSEVO_DIAL_SUPPRESSION_LIST_URL
is unset — matches wallet_check.py / memory_webhook.py: the Dograh OSS
distribution runs with this integration off by default.
"""

import os
import re
from typing import Optional

import httpx
import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL

_TIMEOUT = 2.0

_redis_client: Optional[aioredis.Redis] = None


def _normalize_for_lookup(phone_number: str) -> str:
    """Match Supabase's dial_suppression.phone_key format: digits only, no
    leading '+'. Dograh's phone_number is E.164 (+15095551234); Supabase
    strips everything but digits when writing phone_key. Both sides of this
    comparison must agree on one canonical shape or suppression checks
    silently never match."""
    return re.sub(r"\D", "", phone_number)


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def _check_via_supabase(workflow_id: int, phone_number: str) -> bool:
    """Direct lookup for the one number at hand. Only reached when Redis itself
    raised — a normal negative Redis answer is trusted and never reaches here."""
    check_url = os.getenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL")
    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    headers = {"X-Sysevo-Secret": secret} if secret else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            check_url,
            params={"mode": "check", "workflow_id": workflow_id, "phone": phone_number},
            headers=headers,
        )
    response.raise_for_status()
    data = response.json()
    return bool(data.get("suppressed", False))


async def is_number_suppressed(workflow_id: int, phone_number: str) -> bool:
    """True if the dispatcher must not dial this number for this workflow."""
    if not os.getenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL"):
        return False

    normalized = _normalize_for_lookup(phone_number)

    try:
        r = await _get_redis()
        return bool(await r.sismember(f"suppress:{workflow_id}", normalized))
    except Exception as redis_error:
        logger.warning(
            f"[dial-suppression] Redis unavailable for workflow {workflow_id}, "
            f"falling back to Supabase: {redis_error}"
        )
        try:
            return await _check_via_supabase(workflow_id, normalized)
        except Exception as supabase_error:
            logger.error(
                f"[dial-suppression] Supabase fallback also failed for workflow "
                f"{workflow_id}; skipping the dial rather than guessing it is safe: "
                f"{supabase_error}"
            )
            return True
