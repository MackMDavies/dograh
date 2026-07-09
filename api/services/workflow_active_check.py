"""Sysevo agent activation gate.

Before creating any workflow run (inbound telephony, outbound, WebRTC,
campaign batch, widget), call `check_workflow_active(workflow_id)`. Returns
(allowed, reason). Blocks any non-active status so future states are
safe-by-default. Mirrors `check_wallet_before_call`.
"""

from api.db import db_client
from api.enums import WorkflowStatus


async def check_workflow_active(workflow_id: int) -> tuple[bool, str | None]:
    """Return (allowed, reason).

    allowed=True  → proceed with call creation
    allowed=False → reject; reason is "not_found" or "workflow_<status>"
    """
    workflow = await db_client.get_workflow_by_id(workflow_id)
    if not workflow:
        return False, "not_found"
    if workflow.status != WorkflowStatus.ACTIVE.value:
        return False, f"workflow_{workflow.status}"
    return True, None
