"""Project provider-grounded execution state into the dialer request ledger."""
import asyncio

from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import dialer_agent_history_client as history_db
from api.services.telephony.dialer.agent_history import sync_binding


async def reconcile_dialer_agent_history(_ctx) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        rows = await history_db.rpc("dialer_agent_history_pending",{})
        if not isinstance(rows,list):
            return
        # Bound concurrent database reads independently of the worker's global
        # queue so a busy floor cannot consume every workflow DB connection.
        slots = asyncio.Semaphore(5)
        async def sync(row):
            async with slots:
                try:
                    binding = await history_db.rpc("dialer_agent_history_binding",{"p_call_id":row["id"],"p_run_id":row["workflow_run_id"]})
                    if isinstance(binding,dict):
                        await sync_binding(binding)
                except Exception as exc:
                    logger.warning(f"Agent history reconciliation deferred: {type(exc).__name__}")
                    # Give later requests a turn if one legacy binding cannot
                    # be reconciled. This records a check, never a call state.
                    try:
                        await history_db.rpc("dialer_apply_agent_history_state", {
                            "p_call_id":row["id"], "p_run_id":row["workflow_run_id"], "p_status":None,
                        })
                    except Exception:
                        pass
        await asyncio.gather(*(sync(row) for row in rows))
    except Exception as exc:
        logger.warning(f"Agent history reconciliation unavailable: {type(exc).__name__}")
