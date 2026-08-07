"""Sysevo per-number dial-permission gate.

Before dispatching any outbound call, call `check_dial_permitted(workflow_id,
to_number)`. Returns (allowed, reason). allowed=False means this specific
number should be skipped — either it's under an explicit suppression
(DNC/manual) or it has hit two-plus consecutive machine-answered outcomes
with no human reached since, which needs a better number, not another redial.

Reuses the same endpoint and env vars as wallet_check.py
(SYSEVO_PRE_CALL_CHECK_URL, SYSEVO_MEMORY_SECRET) — dograh-pre-call-check
answers both the wallet and the dial-permission question from the same
to_number-bearing payload, so no new URL needs wiring up.

No-ops silently if SYSEVO_PRE_CALL_CHECK_URL is not set — this allows the
Dograh OSS distribution to run without this enforcement.
"""

import os

import httpx
from loguru import logger

_TIMEOUT = 8.0


async def check_dial_permitted(workflow_id: int, to_number: str) -> tuple[bool, str]:
    """Return (allowed, reason).

    allowed=True  → proceed with dialing
    allowed=False → skip this number; reason is "suppressed" or "needs_enrichment"
    """
    check_url = os.getenv("SYSEVO_PRE_CALL_CHECK_URL")
    if not check_url:
        return True, ""

    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")

    payload = {
        "call_inbound": {
            "agent_id": workflow_id,
            "to_number": to_number,
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
                f"[dial-permission] HTTP {response.status_code} for workflow {workflow_id} — allowing call"
            )
            return True, ""

        data = response.json()
        dynamic_vars = data.get("call_inbound", {}).get("dynamic_variables", {})
        blocked = dynamic_vars.get("dial_blocked", "false") == "true"
        reason = dynamic_vars.get("dial_block_reason", "")

        if blocked:
            logger.warning(
                f"[dial-permission] workflow {workflow_id} number {to_number} blocked: {reason}"
            )
            return False, reason

        return True, ""

    except httpx.TimeoutException:
        logger.warning(f"[dial-permission] timed out for workflow {workflow_id} — allowing call")
        return True, ""
    except Exception as e:
        logger.error(f"[dial-permission] error for workflow {workflow_id}: {e} — allowing call")
        return True, ""
