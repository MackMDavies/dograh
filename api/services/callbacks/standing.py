"""Callbacks run on a "standing" campaign per agent: a queue that never completes,
holding one queued run per promised callback (enqueued by Sysevo's schedule-callback
action). Standing campaigns used to be created by hand in SQL, so only one agent had
one; ensure_standing_campaign creates it on first use for any agent.

A callback that is still unanswered after its last retry is reported to Sysevo
(dograh-callback-failed), which turns it into a task for the team.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Optional

import httpx
from loguru import logger

from api.db import db_client

# Callbacks retry like a person would: not two minutes later, but after a while.
CALLBACK_RETRY_CONFIG = {
    "enabled": True,
    "max_retries": 2,
    "retry_delay_seconds": 1800,
    "retry_on_busy": True,
    "retry_on_no_answer": True,
    "retry_on_voicemail": True,
}


async def ensure_standing_campaign(workflow: Any) -> tuple[Any, bool]:
    """(campaign, created). Idempotent per workflow."""
    existing = await db_client.get_standing_campaign_for_workflow(workflow.id)
    if existing:
        return existing, False
    campaign = await db_client.create_campaign(
        name=f"Callbacks · {workflow.name}"[:255],
        workflow_id=workflow.id,
        source_type="standing",
        source_id=f"standing:{workflow.id}",
        user_id=workflow.user_id,
        organization_id=workflow.organization_id,
        retry_config=dict(CALLBACK_RETRY_CONFIG),
        # The org's own hours (and the state rules) apply to every callback dial.
        calling_hours_config={"mode": "inherit"},
    )
    campaign = await db_client.update_campaign(
        campaign.id, is_standing=True, state="running", started_at=datetime.now(UTC)
    )
    logger.info(f"Created standing callback campaign {campaign.id} for workflow {workflow.id}")
    return campaign, True


def callback_instruction(context: dict) -> Optional[str]:
    """What the agent needs to know when this call IS the callback it promised."""
    if not isinstance(context, dict) or not context.get("origin_workflow_run_id"):
        return None
    reason = str(context.get("reason") or "").strip()
    summary = str(context.get("callback_summary") or "").strip()
    phrase = str(context.get("callback_raw_phrase") or "").strip()
    if not (reason or summary or phrase):
        return None
    lines = [
        "This call is a callback the person asked for on an earlier call. Open by "
        "reminding them you are calling back as promised, and pick up where you left "
        "off; do not make them explain themselves again."
    ]
    if reason:
        lines.append(f"Why they wanted a call back: {reason[:300]}")
    if phrase:
        lines.append(f'What they said: "{phrase[:200]}"')
    if summary:
        lines.append(f"Notes from the earlier call: {summary[:1200]}")
    return "\n".join(lines)


def _hook_url() -> Optional[str]:
    from api.services.sysevo_functions import sysevo_function_url

    return sysevo_function_url("dograh-callback-failed", "SYSEVO_CALLBACK_FAILED_URL")


async def report_final_failure(campaign: Any, queued_run: Any, reason: str) -> None:
    """Tell Sysevo a callback was never answered, so a person follows it up. Never raises."""
    if not getattr(campaign, "is_standing", False):
        return
    url, secret = _hook_url(), os.getenv("SYSEVO_MEMORY_SECRET", "")
    if not url or not secret:
        return
    context = dict(getattr(queued_run, "context_variables", None) or {})
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                headers={"X-Sysevo-Secret": secret},
                json={
                    "workflow_id": campaign.workflow_id,
                    "campaign_id": campaign.id,
                    "queued_run_id": queued_run.id,
                    "attempts": (getattr(queued_run, "retry_count", 0) or 0) + 1,
                    "last_reason": reason,
                    "phone_number": context.get("phone_number"),
                    "first_name": context.get("first_name"),
                    "company": context.get("company"),
                    "reason": context.get("reason"),
                    "origin_workflow_run_id": context.get("origin_workflow_run_id"),
                },
            )
        if response.status_code >= 300:
            logger.warning(f"[callbacks] failed-callback hook returned {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[callbacks] failed-callback hook error (non-fatal): {exc}")
