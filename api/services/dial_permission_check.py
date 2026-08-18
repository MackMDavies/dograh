"""Sysevo per-number dial-permission gate.

Before dispatching any outbound call, call `check_dial_permitted(workflow_id,
to_number)`. Returns (allowed, reason, retry_at). allowed=False means this
specific number should be skipped:
  - "suppressed" / "needs_enrichment" — an explicit DNC/manual suppression, or
    two-plus consecutive machine-answered outcomes needing a better number.
    retry_at is None for these; the caller should not retry automatically.
  - "outside_calling_hours" — outside the effective calling-hours window for
    this contact right now. retry_at is an ISO 8601 UTC timestamp for when the
    window next opens; the caller should defer, not fail, this attempt.
  - "check_unavailable" — the gate could not reach a verdict (timeout, non-2xx,
    or an unhandled error). Fails CLOSED: retry_at is a short backoff and the
    caller should defer exactly as it does for calling hours. Previously this
    path allowed the call, which meant a Supabase blip silently disabled DNC
    and calling-hours enforcement — the permissive answer is byte-identical to
    a genuine "this number is fine". Set SYSEVO_DIAL_CHECK_FAIL_OPEN=true to
    restore that behaviour deliberately.

Pass `campaign_calling_hours` when calling from a campaign context that has
its own calling-hours override (resolved from the campaign's
orchestrator_metadata by the caller) — e.g.
{"mode": "custom", "start": "09:00", "end": "18:00"} or {"mode": "off"}.
Omit it entirely for non-campaign contexts; the check then falls back to the
account's own default.

Reuses the same endpoint and env vars as wallet_check.py
(SYSEVO_PRE_CALL_CHECK_URL, SYSEVO_MEMORY_SECRET) — dograh-pre-call-check
answers the wallet, dial-permission, and calling-hours questions from the
same to_number-bearing payload, so no new URL needs wiring up.

No-ops silently if SYSEVO_PRE_CALL_CHECK_URL is not set — this allows the
Dograh OSS distribution to run without this enforcement.
"""

import os
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx
from loguru import logger

_TIMEOUT = 8.0

# How long to defer a number when the check itself could not be completed.
# Short enough that a brief Supabase blip barely dents throughput, long enough
# that a sustained outage isn't hammered on every dispatcher tick.
_UNAVAILABLE_DEFER_SECONDS = 300

# Reason returned when the gate could not reach a verdict. The dispatcher
# treats it like "outside_calling_hours" — defer with a retry_at, never fail
# the row — so an infrastructure wobble costs a few minutes, not the lead.
REASON_CHECK_UNAVAILABLE = "check_unavailable"


def _defer_response() -> tuple[bool, str, Optional[str]]:
    """Fail closed: don't dial, come back shortly.

    Set SYSEVO_DIAL_CHECK_FAIL_OPEN=true to restore the old behaviour of
    dialling anyway when the check breaks. That trades a compliance risk
    (calling a suppressed number, or outside legal calling hours, because the
    gate was unreachable) for dial throughput during an outage — a deliberate
    choice, so it has to be made explicitly.
    """
    if os.getenv("SYSEVO_DIAL_CHECK_FAIL_OPEN", "").strip().lower() == "true":
        return True, "", None
    retry_at = (
        datetime.now(UTC) + timedelta(seconds=_UNAVAILABLE_DEFER_SECONDS)
    ).isoformat()
    return False, REASON_CHECK_UNAVAILABLE, retry_at


async def check_dial_permitted(
    workflow_id: int,
    to_number: str,
    campaign_calling_hours: Optional[dict] = None,
) -> tuple[bool, str, Optional[str]]:
    """Return (allowed, reason, retry_at).

    allowed=True  → proceed with dialing
    allowed=False → skip this number; reason is "suppressed", "needs_enrichment",
                     or "outside_calling_hours" (only this last one sets retry_at)
    """
    check_url = os.getenv("SYSEVO_PRE_CALL_CHECK_URL")
    if not check_url:
        return True, "", None

    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")

    call_inbound: dict = {
        "agent_id": workflow_id,
        "to_number": to_number,
    }
    if campaign_calling_hours is not None:
        call_inbound["calling_hours"] = campaign_calling_hours

    payload = {"call_inbound": call_inbound}
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Sysevo-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(check_url, json=payload, headers=headers)

        if not response.is_success:
            logger.warning(
                f"[dial-permission] HTTP {response.status_code} for workflow {workflow_id} — deferring call"
            )
            return _defer_response()

        data = response.json()
        dynamic_vars = data.get("call_inbound", {}).get("dynamic_variables", {})
        blocked = dynamic_vars.get("dial_blocked", "false") == "true"
        reason = dynamic_vars.get("dial_block_reason", "")
        retry_at = dynamic_vars.get("retry_at") or None

        if blocked:
            logger.warning(
                f"[dial-permission] workflow {workflow_id} number {to_number} blocked: {reason}"
            )
            return False, reason, retry_at

        return True, "", None

    except httpx.TimeoutException:
        logger.warning(f"[dial-permission] timed out for workflow {workflow_id} — deferring call")
        return _defer_response()
    except Exception as e:
        logger.error(f"[dial-permission] error for workflow {workflow_id}: {e} — deferring call")
        return _defer_response()
