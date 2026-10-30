"""Send an account's configured booking link after an unanswered inbound call."""

from __future__ import annotations

from loguru import logger

from api.services.phone_tools.builtin import send_missed_call_booking_text


async def process_missed_call_text(
    ctx,
    workflow_id: int,
    workflow_run_id: int | str,
    phone_number_id: int,
    caller_number: str,
    called_number: str,
) -> None:
    """Send via Sysevo's tenant, opt-out, wallet and SMS-idempotency controls."""
    result = await send_missed_call_booking_text(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        phone_number_id=phone_number_id,
        caller_number=caller_number,
        called_number=called_number,
    )
    if result.get("status") == "failed":
        # ARQ retries transient hook failures. Sysevo uses a stable request key,
        # so retrying after an accepted send cannot send a second SMS.
        raise RuntimeError("Missed-call text could not be sent")
    if result.get("status", "").startswith("blocked"):
        logger.info("Missed-call text skipped by Sysevo sending policy")
