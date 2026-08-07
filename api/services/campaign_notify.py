"""Notify a campaign's creator, via Supabase, when their scheduled launch fires.

Mirrors services/memory_webhook.py's pattern exactly: a dedicated SYSEVO_*_URL
env var, the shared secret header, and errors are logged but never raised —
a failed notification must never fail the campaign launch itself.
"""
import os

import httpx
from loguru import logger

from api.db import db_client

_TIMEOUT = 10.0


async def notify_campaign_scheduled_started(campaign_id: int) -> None:
    notify_url = os.getenv("SYSEVO_CAMPAIGN_NOTIFY_URL")
    if not notify_url:
        return

    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")

    try:
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            return

        user = await db_client.get_user_by_id(campaign.created_by)
        # UserModel.provider_id IS the Supabase auth user UUID directly (no
        # prefix, unlike organizations.provider_id's "supabase_org_acct_"
        # scheme) — confirmed against live data before this was written.
        if not user or not user.provider_id:
            logger.warning(
                f"[campaign-notify] campaign {campaign_id}: no resolvable Supabase user"
            )
            return

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                notify_url,
                headers={"X-Sysevo-Secret": secret, "Content-Type": "application/json"},
                json={
                    "user_id": user.provider_id,
                    "title": f'Campaign "{campaign.name}" has started',
                    "message": "Your scheduled launch time arrived and dialling has begun.",
                    "link": f"/voice/campaigns/{campaign.id}",
                },
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"[campaign-notify] campaign {campaign_id}: notify endpoint returned "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
    except Exception as e:
        logger.warning(f"[campaign-notify] campaign {campaign_id} failed (non-fatal): {e}")
