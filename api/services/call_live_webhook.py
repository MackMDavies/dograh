"""Sysevo live-call registry webhook.

The Sysevo `active_calls` table holds one row per in-progress call so the client
voice area and the admin Command Center can show live calls. Rows are inserted at
call-start by the Dograh pre-call edge hooks and normally deleted at call-end by
the dograh-post-call-memory edge function — but that post-call clear only runs for
telephony calls (which carry a caller_number). Web / widget preview calls have no
caller_number, so their live row would otherwise linger until the 2-hour TTL sweep.

This module fires an unconditional "ended" event to SYSEVO_CALL_LIVE_URL so the row
clears immediately for those calls too. Purely best-effort: silently no-ops if the
URL is not configured, and never raises (the caller in s3_upload also guards it).
"""

import os
from typing import Any

import httpx
from loguru import logger

_TIMEOUT = 15.0


async def fire_call_live(event: str, workflow_run_id: int) -> None:
    """POST a live-call lifecycle event to the Sysevo edge function.

    Currently only "ended" is fired (from post-call processing) to clear the
    active_calls row. No-ops if SYSEVO_CALL_LIVE_URL is unset, so an unwired
    deployment behaves exactly as before (rows reaped by the TTL sweeper).
    """
    call_live_url = os.getenv("SYSEVO_CALL_LIVE_URL")
    if not call_live_url:
        return

    secret = os.getenv("SYSEVO_CALL_LIVE_SECRET") or os.getenv("SYSEVO_MEMORY_SECRET", "")

    payload: dict[str, Any] = {
        "event": event,
        "workflow_run_id": workflow_run_id,
        "run_id": workflow_run_id,
    }

    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Sysevo-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(call_live_url, json=payload, headers=headers)
        if response.is_success:
            logger.info(
                f"[call-live] run {workflow_run_id} event={event} "
                f"→ cleared ({response.status_code})"
            )
        else:
            logger.warning(
                f"[call-live] run {workflow_run_id} HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
    except httpx.TimeoutException:
        logger.warning(f"[call-live] timed out for run {workflow_run_id}")
    except Exception as e:
        logger.error(f"[call-live] error for run {workflow_run_id}: {e}")
