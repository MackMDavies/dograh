"""Sysevo wallet pre-call gate.

Before creating any workflow run (inbound telephony, outbound, WebRTC), call
`check_wallet_before_call(workflow_id)`. Returns True if the call should
proceed, False if it should be blocked (insufficient balance / suspended).

No-ops silently if SYSEVO_PRE_CALL_CHECK_URL is not set — this allows
the Dograh OSS distribution to run without billing enforcement.
"""

import os

import httpx
from loguru import logger

_TIMEOUT = 8.0


async def check_wallet_before_call(workflow_id: int) -> tuple[bool, str]:
    """Return (allowed, reason).

    allowed=True  → proceed with call creation
    allowed=False → reject the call; reason gives the block cause
    """
    check_url = os.getenv("SYSEVO_PRE_CALL_CHECK_URL")
    if not check_url:
        return True, ""

    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")

    payload = {
        "call_inbound": {
            "agent_id": workflow_id,
        }
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Sysevo-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(check_url, json=payload, headers=headers)

        if not response.is_success:
            logger.warning(
                f"[wallet-check] HTTP {response.status_code} for workflow {workflow_id} — allowing call"
            )
            return True, ""

        data = response.json()
        dynamic_vars = data.get("call_inbound", {}).get("dynamic_variables", {})
        blocked = dynamic_vars.get("wallet_blocked", "false") == "true"
        reason = dynamic_vars.get("wallet_block_reason", "insufficient_balance")

        if blocked:
            logger.warning(
                f"[wallet-check] workflow {workflow_id} blocked: {reason}"
            )
            return False, reason

        return True, ""

    except httpx.TimeoutException:
        logger.warning(f"[wallet-check] timed out for workflow {workflow_id} — allowing call")
        return True, ""
    except Exception as e:
        logger.error(f"[wallet-check] error for workflow {workflow_id}: {e} — allowing call")
        return True, ""
