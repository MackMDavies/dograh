"""Sysevo API-billing pre-call gate.

Before creating an API-key-initiated workflow run, checks whether the payer's API
billing account has a usable card. Returns (allowed, reason).

Fail-open / no-op when the gate URL is not configured or on any error — a billing
gate must never take down call creation due to an infra hiccup. Enforcement is
additionally gated server-side by API_GATING_ENABLED on the edge function.
"""

import os

import httpx
from loguru import logger

_TIMEOUT = 8.0


async def check_api_billing_gate(api_key_id: int) -> tuple[bool, str]:
    """Return (allowed, reason). allowed=False → reject the API-initiated call."""
    url = os.getenv("SYSEVO_API_BILLING_PRECHECK_URL")
    if not url:
        for _env in (
            "SYSEVO_PRE_CALL_CHECK_URL",
            "SYSEVO_WALLET_DEBIT_URL",
            "SYSEVO_POST_CALL_MEMORY_URL",
            "SYSEVO_MEMORY_PRE_CALL_URL",
        ):
            _u = os.getenv(_env)
            if _u:
                url = f"{_u.rsplit('/', 1)[0]}/api-billing-precheck"
                break
    if not url:
        return True, ""

    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Sysevo-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json={"api_key_id": api_key_id}, headers=headers)
        if not resp.is_success:
            return True, ""  # fail-open
        data = resp.json()
        if data.get("allowed", True):
            return True, ""
        reason = data.get("reason", "billing_card_required")
        logger.warning(f"[api-billing-gate] api_key {api_key_id} blocked: {reason}")
        return False, reason
    except Exception as e:
        logger.error(f"[api-billing-gate] error for key {api_key_id}: {e} — allowing")
        return True, ""
