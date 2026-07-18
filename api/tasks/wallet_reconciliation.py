"""Post-call wallet debit reconciliation (safety-net cron).

The normal post-call debit (api/services/wallet_webhook.py, fired from
process_workflow_completion) is best-effort: if a call crashes before the completion
job is enqueued, or the debit webhook fails transiently, the run is left with
wallet_debit_settled_at IS NULL and never charged.

This ARQ cron re-fires the debit for such runs. Re-firing is double-charge-safe:
the wallet-debit edge function and the wallet_debit RPC are both idempotent on
workflow_run_id (see supabase/migrations/20260717000000_wallet_debit_idempotency.sql),
so a run that WAS actually charged is a no-op on retry — we just mark it settled.

Only wallet runs are reconciled (api_key_id IS NULL); API-key runs bill postpaid via a
separate mechanism with its own reconcile job (api-billing-reconcile).
"""

import os

from loguru import logger

from api.db import db_client
from api.services.wallet_webhook import fire_post_call_wallet_debit

# Same env flags used by process_workflow_completion to decide the Sysevo integration
# is active. If none are set, this deployment does no Sysevo billing and the cron no-ops.
_SYSEVO_ENV_VARS = (
    "SYSEVO_WALLET_DEBIT_URL",
    "SYSEVO_POST_CALL_MEMORY_URL",
    "SYSEVO_PRE_CALL_CHECK_URL",
    "SYSEVO_MEMORY_PRE_CALL_URL",
)


async def reconcile_wallet_debits(_ctx) -> None:
    """Re-fire the post-call debit for completed wallet runs that never settled."""
    if not any(os.getenv(v) for v in _SYSEVO_ENV_VARS):
        return  # Sysevo billing integration not active on this deployment

    try:
        run_ids = await db_client.get_unsettled_wallet_debit_run_ids()
    except Exception as e:
        logger.error(f"[wallet-reconcile] failed to list unsettled runs: {e}")
        return

    if not run_ids:
        return

    logger.info(f"[wallet-reconcile] reconciling {len(run_ids)} unsettled run(s)")
    settled = 0
    for run_id in run_ids:
        try:
            # release_slot=False: the concurrency slot was already released at original
            # completion; re-releasing would corrupt the shared counter.
            is_settled = await fire_post_call_wallet_debit(run_id, release_slot=False)
            if is_settled:
                await db_client.mark_wallet_debit_settled(run_id)
                settled += 1
        except Exception as e:
            logger.error(f"[wallet-reconcile] run {run_id} failed: {e}")

    logger.info(f"[wallet-reconcile] settled {settled}/{len(run_ids)} run(s)")
