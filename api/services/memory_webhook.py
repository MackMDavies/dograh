"""Sysevo caller memory post-call webhook.

After every call completes, this fires a POST to the Sysevo
dograh-post-call-memory edge function with the call transcript and metadata.
Sysevo then runs LLM extraction (Claude Haiku) to pull out caller name, email,
address, preferences, and other facts, updating caller_memory and
caller_memory_facts in Supabase.

Called non-fatally from process_workflow_completion — any error is logged but
never bubbles up to crash the completion pipeline.
"""

import os
from typing import Any

import httpx
from loguru import logger

from api.db import db_client
from api.utils.transcript import generate_transcript_text

_TIMEOUT = 30.0


async def fire_post_call_memory(workflow_run_id: int) -> bool:
    """Send post-call data to Sysevo memory extraction endpoint.

    Reads transcript from workflow_run.logs['realtime_feedback_events'], which
    is persisted before process_workflow_completion runs.

    Returns a *settlement* flag for the reconciliation sweep (H4):
      - True  => terminal; do NOT retry. Covers: no Sysevo memory URL configured, run
                 missing, no caller_number (nothing to store), HTTP 2xx, and HTTP 4xx.
      - False => transient failure (timeout, HTTP 5xx, network/exception); the
                 reconcile_memory cron should re-fire this run later.
    """
    memory_url = os.getenv("SYSEVO_POST_CALL_MEMORY_URL")
    if not memory_url:
        return True

    memory_secret = os.getenv("SYSEVO_MEMORY_SECRET", "")

    try:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        if not workflow_run:
            logger.warning(f"[memory-post-call] run {workflow_run_id} not found")
            return True

        initial_ctx: dict[str, Any] = workflow_run.initial_context or {}
        # Which party is the LEAD depends on direction, and getting this wrong is
        # catastrophic rather than merely wrong. On an outbound campaign call
        # `caller_number` is OUR OWN outbound caller ID (+1659…) — byte-identical
        # on every run — so keying caller memory on it collapses every lead in
        # the campaign into a single shared profile. The person we dialled is the
        # one worth remembering. On inbound the reverse holds: they rang us.
        is_outbound = str(getattr(workflow_run, "call_type", "") or "").lower() == "outbound"
        if is_outbound:
            caller_number: str | None = (
                initial_ctx.get("called_number")
                or initial_ctx.get("phone_number")
                or initial_ctx.get("caller_number")
            )
        else:
            caller_number = (
                initial_ctx.get("caller_number")
                or initial_ctx.get("called_number")
                or initial_ctx.get("phone_number")
            )

        # NOTE: this used to `return True` (terminal, do not retry) whenever
        # caller_number was absent. On this deployment initial_context carries
        # neither caller_number nor called_number for any run, so that guard
        # silently suppressed EVERY post-call notification ever sent — no
        # analysis, no caller memory, and nothing in the Sysevo Inbound inbox,
        # with no error anywhere because the skip reports success.
        #
        # A missing number is not a reason to drop the call. The Sysevo endpoint
        # keys on the run when there is no phone number (web:run:<id>) and the
        # transcript is what analysis actually needs. Post it regardless.

        # Build transcript from realtime feedback events stored in logs
        logs: dict[str, Any] = workflow_run.logs or {}
        feedback_events: list[dict] = logs.get("realtime_feedback_events", [])
        transcript = generate_transcript_text(feedback_events)

        gathered: dict[str, Any] = workflow_run.gathered_context or {}
        call_summary: str | None = (
            gathered.get("call_summary")
            or gathered.get("summary")
            or None
        )

        # Duration: use usage_info if available, else None
        usage: dict[str, Any] = workflow_run.usage_info or {}
        duration_secs: int | None = (
            usage.get("duration_seconds")
            or usage.get("call_duration_seconds")
            or None
        )

        # The mapped call disposition (authoritative outcome for this call);
        # Sysevo writes it into caller_memory.last_outcome.
        disposition: str | None = gathered.get("mapped_call_disposition") or None

        payload: dict[str, Any] = {
            "run_id": workflow_run_id,
            "workflow_id": workflow_run.workflow_id,
            "caller_number": caller_number,
            "transcript": transcript,
            "call_summary": call_summary,
            "disposition": disposition,
            "duration_secs": duration_secs,
            "gathered_context": gathered,
            # The campaign row we dialled FROM: first_name, company, email,
            # phone_number and any other CSV columns. On an outbound cold call we
            # already know who we rang — there is no need to infer identity from
            # the transcript, and inference fails on exactly the short calls that
            # dominate cold outreach. Without this the receiving endpoint has no
            # name, its name gate rejects the row, and no caller profile is ever
            # created. Sent whole so new CSV columns flow through without a
            # change here.
            "initial_context": initial_ctx,
            "call_type": getattr(workflow_run, "call_type", None),
        }

        headers = {
            "Content-Type": "application/json",
        }
        if memory_secret:
            headers["X-Sysevo-Secret"] = memory_secret

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(memory_url, json=payload, headers=headers)

        if response.is_success:
            logger.info(
                f"[memory-post-call] run {workflow_run_id} caller=***{str(caller_number)[-4:]} "
                f"→ memory updated ({response.status_code})"
            )
            return True
        elif 400 <= response.status_code < 500:
            logger.warning(
                f"[memory-post-call] run {workflow_run_id} HTTP {response.status_code} "
                f"(terminal): {response.text[:200]}"
            )
            return True
        else:
            logger.warning(
                f"[memory-post-call] run {workflow_run_id} HTTP {response.status_code} "
                f"(will retry): {response.text[:200]}"
            )
            return False

    except httpx.TimeoutException:
        logger.warning(f"[memory-post-call] timed out for run {workflow_run_id} (will retry)")
        return False
    except Exception as e:
        logger.error(f"[memory-post-call] error for run {workflow_run_id} (will retry): {e}")
        return False
