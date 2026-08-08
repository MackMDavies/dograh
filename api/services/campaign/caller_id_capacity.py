"""How many simultaneous calls a single caller ID (CLI) may carry.

The from_number pool used to be strictly exclusive: one live call per configured
number. Combined with the campaign validator taking
``min(org_limit, phone_number_count)``, an organisation with one DID was pinned
to a concurrency of 1 no matter what its plan allowed — and the error told the
user to buy more numbers.

Carriers do not cap concurrent originations per DID, so the exclusivity was a
caller-ID *rotation* policy, not a technical constraint. It is now opt-in:

- ``None`` (the default) means no per-CLI cap. Total concurrency is bounded by
  the org's ``CONCURRENT_CALL_LIMIT`` alone; the pool still rotates evenly
  across whatever numbers exist.
- A positive integer caps simultaneous calls per number, so the effective
  campaign ceiling becomes ``min(org_limit, numbers * calls_per_number)``.

Resolution order: per-org ``CALLS_PER_NUMBER`` configuration, then the
``DEFAULT_CALLS_PER_NUMBER`` env default. A value <= 0 in either place means
unlimited.
"""

from loguru import logger

from api.constants import DEFAULT_CALLS_PER_NUMBER
from api.db import db_client
from api.enums import OrganizationConfigurationKey


def _coerce(raw) -> int | None:
    """Normalise a configured value to a positive cap, or None for unlimited."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def env_calls_per_number() -> int | None:
    """The deployment-wide default, or None when no per-CLI cap is configured."""
    return _coerce(DEFAULT_CALLS_PER_NUMBER)


async def get_calls_per_number(organization_id: int) -> int | None:
    """Simultaneous calls allowed per caller ID for this org.

    Returns None when there is no per-CLI cap (the default).
    """
    try:
        config = await db_client.get_configuration(
            organization_id,
            OrganizationConfigurationKey.CALLS_PER_NUMBER.value,
        )
        if config and config.value:
            configured = _coerce(config.value.get("value"))
            if configured is not None:
                return configured
            # An explicit 0/negative means "unlimited", and must not fall
            # through to a stricter env default.
            if config.value.get("value") is not None:
                return None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            f"[caller-id-capacity] CALLS_PER_NUMBER lookup failed for org "
            f"{organization_id}: {e}"
        )
    return env_calls_per_number()


def effective_concurrency_limit(
    org_limit: int, from_numbers_count: int, calls_per_number: int | None
) -> int:
    """The highest max_concurrency a campaign may be given.

    Caller-ID supply only constrains concurrency when a per-CLI cap is
    configured AND at least one number is known. With no cap (default), or when
    the number count could not be determined, the org limit governs alone.
    """
    if calls_per_number and from_numbers_count > 0:
        return min(org_limit, from_numbers_count * calls_per_number)
    return org_limit
