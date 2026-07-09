"""Sysevo API-key payer-link webhook.

When a user creates a Dograh API key, fires a POST to the Sysevo `link-api-key`
edge function so the payer mapping (dograh_api_key_id -> client_account) is written
server-side and authoritatively (Dograh vouches the key belongs to this user).

Called non-fatally from the create_api_key route — any error is logged but never
blocks key creation.

Silently no-ops if no Sysevo edge-function URL is configured (same env flags that
signal the Sysevo integration is active).
"""

import os

import httpx
from loguru import logger

_TIMEOUT = 10.0


def _resolve_link_url() -> str | None:
    """Resolve the link-api-key edge-function URL from env (dedicated var or derived)."""
    url = os.getenv("SYSEVO_LINK_API_KEY_URL")
    if url:
        return url
    # Derive from any configured Sysevo edge-function URL — all share the same base.
    for _env in (
        "SYSEVO_WALLET_DEBIT_URL",
        "SYSEVO_POST_CALL_MEMORY_URL",
        "SYSEVO_PRE_CALL_CHECK_URL",
        "SYSEVO_MEMORY_PRE_CALL_URL",
    ):
        _url = os.getenv(_env)
        if _url:
            return f"{_url.rsplit('/', 1)[0]}/link-api-key"
    return None


async def fire_api_key_link(
    provider_id: str,
    dograh_api_key_id: int,
    dograh_key_prefix: str | None = None,
) -> None:
    """POST the payer link to the Sysevo link-api-key edge function.

    provider_id is the Dograh UserModel.provider_id, which under Supabase auth equals
    the Supabase auth.uid() the edge function resolves to a client_account.
    Non-fatal: logs and returns on any failure.
    """
    link_url = _resolve_link_url()
    if not link_url:
        return

    memory_secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if memory_secret:
        headers["X-Sysevo-Secret"] = memory_secret

    payload = {
        "provider_id": provider_id,
        "dograh_api_key_id": dograh_api_key_id,
        "dograh_key_prefix": dograh_key_prefix,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(link_url, json=payload, headers=headers)
        if response.is_success:
            logger.info(
                f"[link-api-key] linked key={dograh_api_key_id} "
                f"(response: {response.json()})"
            )
        else:
            logger.warning(
                f"[link-api-key] key={dograh_api_key_id} HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
    except httpx.TimeoutException:
        logger.warning(f"[link-api-key] timed out for key={dograh_api_key_id}")
    except Exception as e:
        logger.warning(f"[link-api-key] failed for key={dograh_api_key_id}: {e}")
