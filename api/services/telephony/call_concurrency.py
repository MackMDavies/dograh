"""Per-organization concurrent-call slot enforcement for ALL live call paths.

Wraps the campaign `rate_limiter` (Redis, keyed by organization_id) so inbound
(ARI/SIP + Twilio), WebRTC, and web-widget calls share ONE concurrency counter
capped at the org's CONCURRENT_CALL_LIMIT — which the Sysevo billing webhook syncs
from the plan tier (Free 2 / Starter 5 / Growth 10 / Business 20). Outbound campaigns
already use the same limiter directly.

Lifecycle per call:
    allowed, slot = await acquire_call_slot(org_id)   # reject the call if not allowed
    # ... create the workflow run ...
    await bind_slot(run.id, org_id, slot)             # so the end hook can release it
    # ... at call end (fire_post_call_wallet_debit) ...
    await release_call_slot(run.id)

A missed release self-heals via the limiter's stale TTL (20 min), so a leaked slot
can never permanently block an account.
"""

from loguru import logger

from api.constants import DEFAULT_ORG_CONCURRENCY_LIMIT
from api.db import db_client
from api.enums import OrganizationConfigurationKey
from api.services.campaign.rate_limiter import rate_limiter

# Live-audio run modes that occupy a concurrent-call slot. Excludes TEXTCHAT and the
# historical STASIS/VOICE/CHAT modes.
LIVE_CALL_MODES = frozenset(
    {"ari", "plivo", "twilio", "vonage", "vobiz", "cloudonix", "telnyx", "webrtc", "smallwebrtc"}
)


def is_live_call_mode(mode) -> bool:
    """True if a run mode string represents a live audio call (counts against concurrency)."""
    return str(mode).lower() in LIVE_CALL_MODES


async def _org_limit(organization_id: int) -> int:
    try:
        cfg = await db_client.get_configuration(
            organization_id, OrganizationConfigurationKey.CONCURRENT_CALL_LIMIT.value
        )
        if cfg and cfg.value:
            return int(cfg.value["value"])
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[call-concurrency] limit lookup failed for org {organization_id}: {e}")
    return int(DEFAULT_ORG_CONCURRENCY_LIMIT)


async def acquire_call_slot(organization_id: int) -> tuple[bool, str | None]:
    """Return (allowed, slot_id).

    allowed=False → the org is at its concurrent-call limit; reject the call.
    Fails OPEN on any limiter error so infra issues never break calling.
    """
    try:
        limit = await _org_limit(organization_id)
        slot_id = await rate_limiter.try_acquire_concurrent_slot(organization_id, limit)
        if slot_id is None:
            logger.info(f"[call-concurrency] org {organization_id} at limit {limit} — rejecting call")
            return False, None
        return True, slot_id
    except Exception as e:
        logger.error(f"[call-concurrency] acquire failed for org {organization_id}: {e} — allowing")
        return True, None


async def bind_slot(workflow_run_id: int, organization_id: int, slot_id: str | None) -> None:
    """Associate an acquired slot with the created run so it can be released at call end."""
    if not slot_id:
        return
    try:
        await rate_limiter.store_workflow_slot_mapping(workflow_run_id, organization_id, slot_id)
    except Exception as e:
        logger.error(f"[call-concurrency] bind failed for run {workflow_run_id}: {e}")


async def release_slot_for_failed_start(organization_id: int, slot_id: str | None) -> None:
    """Release a slot acquired for a call whose run creation failed (no run to bind)."""
    if not slot_id:
        return
    try:
        await rate_limiter.release_concurrent_slot(organization_id, slot_id)
    except Exception as e:
        logger.error(f"[call-concurrency] failed-start release error for org {organization_id}: {e}")


async def release_call_slot(workflow_run_id: int) -> None:
    """Release the concurrency slot held by a finished call. Call-end hook; idempotent."""
    try:
        mapping = await rate_limiter.get_workflow_slot_mapping(workflow_run_id)
        if not mapping:
            return
        org_id, slot_id = mapping
        await rate_limiter.release_concurrent_slot(org_id, slot_id)
        await rate_limiter.delete_workflow_slot_mapping(workflow_run_id)
    except Exception as e:
        logger.error(f"[call-concurrency] release failed for run {workflow_run_id}: {e}")
