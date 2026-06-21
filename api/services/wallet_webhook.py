"""Sysevo wallet debit post-call webhook.

After every call completes, fires a POST to the Sysevo wallet-debit edge
function to debit the account wallet based on call duration and model selection.

Called non-fatally from process_workflow_completion — any error is logged but
never crashes the completion pipeline.

Non-ops silently if SYSEVO_POST_CALL_MEMORY_URL is not configured (same env
flag signals that the Sysevo integration is active).
"""

import os
from typing import Any

import httpx
from loguru import logger

from api.db import db_client

_TIMEOUT = 15.0


async def fire_post_call_wallet_debit(workflow_run_id: int) -> None:
    """Debit the account wallet for call usage.

    Resolves duration from usage_info, model info from the run config,
    and sends to the Sysevo wallet-debit edge function.

    Silently no-ops if SYSEVO_POST_CALL_MEMORY_URL is not configured.
    """
    memory_url = os.getenv("SYSEVO_POST_CALL_MEMORY_URL")
    if not memory_url:
        return

    # Derive wallet-debit URL from the post-call memory URL base
    base_url = memory_url.rsplit("/", 1)[0]
    debit_url = f"{base_url}/wallet-debit"

    memory_secret = os.getenv("SYSEVO_MEMORY_SECRET", "")

    try:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        if not workflow_run:
            logger.warning(f"[wallet-debit] run {workflow_run_id} not found")
            return

        usage: dict[str, Any] = workflow_run.usage_info or {}
        duration_secs: float | None = (
            usage.get("duration_seconds")
            or usage.get("call_duration_seconds")
            or None
        )

        if not duration_secs or duration_secs < 1:
            logger.debug(f"[wallet-debit] run {workflow_run_id} — no duration, skipping")
            return

        # Extract model info from usage_info or initial_context
        initial_ctx: dict[str, Any] = workflow_run.initial_context or {}
        config: dict[str, Any] = initial_ctx.get("config", {}) or {}

        llm_model: str | None = (
            usage.get("llm_model")
            or config.get("llm_model")
            or None
        )
        llm_provider: str | None = (
            usage.get("llm_provider")
            or config.get("llm_provider")
            or None
        )
        stt_provider: str | None = config.get("stt_provider")
        tts_provider: str | None = config.get("tts_provider")

        payload: dict[str, Any] = {
            "workflow_run_id": workflow_run_id,
            "agent_id": workflow_run.workflow_id,
            "duration_secs": duration_secs,
        }
        if llm_provider:
            payload["llm_provider"] = llm_provider
        if llm_model:
            payload["llm_model"] = llm_model
        if stt_provider:
            payload["stt_provider"] = stt_provider
        if tts_provider:
            payload["tts_provider"] = tts_provider

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if memory_secret:
            headers["X-Sysevo-Secret"] = memory_secret

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(debit_url, json=payload, headers=headers)

        if response.is_success:
            data = response.json()
            amount = data.get("amount_cents", 0)
            logger.info(
                f"[wallet-debit] run {workflow_run_id} duration={duration_secs:.1f}s "
                f"charged={amount/100:.4f} USD"
            )
        else:
            logger.warning(
                f"[wallet-debit] run {workflow_run_id} HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

    except httpx.TimeoutException:
        logger.warning(f"[wallet-debit] timed out for run {workflow_run_id}")
    except Exception as e:
        logger.error(f"[wallet-debit] error for run {workflow_run_id}: {e}")
