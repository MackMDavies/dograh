"""Post-call caller-memory reconciliation (safety-net cron, H4).

The normal post-call memory extraction (api/services/memory_webhook.py, fired from
process_workflow_completion) is best-effort: if a call crashes before the completion
job is enqueued, or the memory webhook fails transiently, the run is left with
memory_settled_at IS NULL and its memory is never extracted.

This ARQ cron re-fires the memory webhook for such runs. Re-firing is safe: the
dograh-post-call-memory edge function dedupes per run (call_history run_id + upsert on
the caller_memory_facts natural key), and the analyze-conversation enqueue is idempotent
(claim_call_analysis + profile_projection_log ON CONFLICT). A run whose memory WAS
recorded is effectively a no-op on retry — we just mark it settled.

Mirrors the wallet-debit reconciliation (api/tasks/wallet_reconciliation.py).
"""

import os

from loguru import logger

from api.db import db_client
from api.services.memory_webhook import fire_post_call_memory

# If the Sysevo memory integration isn't configured on this deployment, the cron no-ops.
_MEMORY_ENV_VAR = "SYSEVO_POST_CALL_MEMORY_URL"


async def reconcile_memory(_ctx) -> None:
    """Re-fire the post-call memory extraction for completed runs that never settled."""
    if not os.getenv(_MEMORY_ENV_VAR):
        return  # Sysevo caller-memory integration not active on this deployment

    try:
        run_ids = await db_client.get_unsettled_memory_run_ids()
    except Exception as e:
        logger.error(f"[memory-reconcile] failed to list unsettled runs: {e}")
        return

    if not run_ids:
        return

    logger.info(f"[memory-reconcile] reconciling {len(run_ids)} unsettled run(s)")
    settled = 0
    for run_id in run_ids:
        try:
            is_settled = await fire_post_call_memory(run_id)
            if is_settled:
                await db_client.mark_memory_settled(run_id)
                settled += 1
        except Exception as e:
            logger.error(f"[memory-reconcile] run {run_id} failed: {e}")

    logger.info(f"[memory-reconcile] settled {settled}/{len(run_ids)} run(s)")
