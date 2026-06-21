"""Sysevo caller memory pre-call fetch.

Fires at call start for any telephony call that has a caller_number in context.
Calls the Sysevo dograh-memory-inbound-hook edge function, which returns
dynamic_variables (caller_name, caller_memory block, etc.) to be merged into
the Pipecat engine's call context vars.

These variables become available as {{caller_name}}, {{caller_memory}}, etc. in
the agent's system prompt template.
"""

import os
from typing import Any

import httpx
from loguru import logger

# Timeout is intentionally shorter than the ringer timeout so we never block a
# call for too long. The event_handlers ringer plays while we wait.
_MEMORY_FETCH_TIMEOUT = 8.0


async def execute_memory_pre_call_fetch(
    *,
    url: str,
    secret: str,
    call_context_vars: dict[str, Any],
    workflow_id: int,
) -> dict[str, Any]:
    """POST caller context to the Sysevo memory hook and return dynamic_variables.

    Returns an empty dict on any error so the call always proceeds.
    """
    payload = {
        "event": "call_inbound",
        "call_inbound": {
            "agent_id": workflow_id,
            "from_number": call_context_vars.get("caller_number", ""),
            "to_number": call_context_vars.get("called_number", ""),
        },
    }
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if secret:
        headers["X-Sysevo-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=_MEMORY_FETCH_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)

        if not response.is_success:
            logger.warning(
                f"[memory-pre-call] HTTP {response.status_code} from memory hook"
            )
            return {}

        data: dict = response.json()

        # Extract dynamic_variables from Dograh response envelope
        call_inbound = data.get("call_inbound", {})
        if isinstance(call_inbound, dict):
            dynamic_vars = call_inbound.get("dynamic_variables", {})
        else:
            dynamic_vars = data.get("dynamic_variables", {})

        if not isinstance(dynamic_vars, dict):
            return {}

        caller_name = dynamic_vars.get("caller_name", "")
        caller_known = dynamic_vars.get("caller_known", "false")
        logger.info(
            f"[memory-pre-call] caller_known={caller_known} name={caller_name!r}"
        )
        return dynamic_vars

    except httpx.TimeoutException:
        logger.warning("[memory-pre-call] Timed out — proceeding without memory")
        return {}
    except Exception as e:
        logger.error(f"[memory-pre-call] Unexpected error: {e}")
        return {}
