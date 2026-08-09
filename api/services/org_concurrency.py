"""One place that answers "how many calls may this organisation run at once?".

Four call sites used to each read CONCURRENT_CALL_LIMIT and fall back to
DEFAULT_ORG_CONCURRENCY_LIMIT on their own (campaign validation, campaign
defaults, the campaign dispatcher, and live-call slot acquisition). They now all
route through :func:`get_org_concurrency_limit`, so the system-wide ceiling is
enforced everywhere rather than in whichever path someone remembered.

MAX_SYSTEM_CONCURRENCY is a hard cap applied on both read and write. Sysevo's
billing webhook pushes 100000 for "unlimited" plan tiers, so without a clamp an
unlimited tier would authorise more simultaneous pipelines than the box can
physically run. Nothing anywhere may exceed it.

Deliberately dependency-light: importing api.services.telephony eagerly pulls in
the provider package and would create a circular import through the campaign
dispatcher, so this module imports only the db client and enums.
"""

from loguru import logger

from api.constants import DEFAULT_ORG_CONCURRENCY_LIMIT, MAX_SYSTEM_CONCURRENCY
from api.db import db_client
from api.enums import OrganizationConfigurationKey


def clamp_to_system_max(value: int) -> int:
    """Bound a concurrency figure to [1, MAX_SYSTEM_CONCURRENCY]."""
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = int(DEFAULT_ORG_CONCURRENCY_LIMIT)
    return max(1, min(coerced, MAX_SYSTEM_CONCURRENCY))


async def get_org_concurrency_limit(organization_id: int) -> int:
    """Simultaneous calls this org may run, already clamped to the system max.

    Falls back to DEFAULT_ORG_CONCURRENCY_LIMIT when the org has no configured
    value or the lookup fails — never raises, because every live call path
    depends on it.
    """
    try:
        config = await db_client.get_configuration(
            organization_id,
            OrganizationConfigurationKey.CONCURRENT_CALL_LIMIT.value,
        )
        if config and config.value:
            return clamp_to_system_max(
                config.value.get("value", DEFAULT_ORG_CONCURRENCY_LIMIT)
            )
    except Exception as e:
        logger.warning(
            f"[org-concurrency] limit lookup failed for org {organization_id}: {e}"
        )
    return clamp_to_system_max(DEFAULT_ORG_CONCURRENCY_LIMIT)
