"""Periodic Redis mirror of Supabase's dial_suppression register (ARQ cron).

Structured identically to api/tasks/wallet_reconciliation.py: no-ops if the
Sysevo integration isn't configured, never raises out of the task on a
transient failure (logged, picked up next cycle).

Rebuild strategy: build each workflow's new set in a scratch key, then RENAME
it over the live key. This is an atomic replace — the live key is never
briefly empty mid-rebuild, which would otherwise create a real window where
every number reads as not-suppressed.
"""

import os
from collections import defaultdict
from typing import Optional

import httpx
import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL

_TIMEOUT = 10.0

_redis_client: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def sync_dial_suppression(_ctx) -> None:
    """Rebuild every workflow's suppress:{workflow_id} Redis set from Supabase."""
    list_url = os.getenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL")
    if not list_url:
        return  # Sysevo dial-suppression integration not active on this deployment

    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    headers = {"X-Sysevo-Secret": secret} if secret else {}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(list_url, params={"mode": "list"}, headers=headers)
        response.raise_for_status()
        rows = response.json().get("suppressions", [])

        by_workflow: dict[int, list[str]] = defaultdict(list)
        for row in rows:
            by_workflow[row["dograh_workflow_id"]].append(row["phone_key"])
    except Exception as e:
        logger.error(f"[dial-suppression-sync] failed to fetch/parse suppression list: {e}")
        return

    try:
        r = await _get_redis()
    except Exception as e:
        logger.error(f"[dial-suppression-sync] failed to connect to Redis: {e}")
        return

    for workflow_id, phones in by_workflow.items():
        scratch_key = f"suppress:{workflow_id}:building"
        live_key = f"suppress:{workflow_id}"
        try:
            await r.delete(scratch_key)
            await r.sadd(scratch_key, *phones)
            await r.rename(scratch_key, live_key)
        except Exception as e:
            logger.error(f"[dial-suppression-sync] failed to rebuild {live_key}: {e}")

    logger.info(
        f"[dial-suppression-sync] rebuilt {len(by_workflow)} workflow suppression set(s), "
        f"{len(rows)} total number(s)"
    )
