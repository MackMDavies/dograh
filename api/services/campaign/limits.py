"""Plan limits shared by interactive and scheduled campaign paths."""

from typing import Optional

from api.db import db_client
from api.enums import OrganizationConfigurationKey


class CampaignLimitConfigurationError(RuntimeError):
    """The plan cap could not be trusted, so campaign creation must stop."""


async def get_active_campaign_limit(
    organization_id: int, *, bypass: bool = False
) -> Optional[int]:
    """Return the tier cap; missing Self-Serve sync data fails closed."""
    if bypass:
        return None
    try:
        plan_config = await db_client.get_configuration(
            organization_id, OrganizationConfigurationKey.SYSEVO_PLAN_KEY.value
        )
        config = await db_client.get_configuration(
            organization_id, OrganizationConfigurationKey.MAX_ACTIVE_CAMPAIGNS.value
        )
    except Exception as e:
        raise CampaignLimitConfigurationError("campaign limit lookup failed") from e
    plan_key = None
    if plan_config is not None:
        if not isinstance(plan_config.value, dict):
            raise CampaignLimitConfigurationError("plan key config is malformed")
        plan_key = plan_config.value.get("value")
        if plan_key is not None and not isinstance(plan_key, str):
            raise CampaignLimitConfigurationError("plan key config is malformed")
    self_serve = isinstance(plan_key, str) and plan_key.startswith("subscription_")
    if config is None:
        if self_serve:
            raise CampaignLimitConfigurationError("Self-Serve campaign cap has not synced")
        return None
    value = config.value
    if not isinstance(value, dict):
        raise CampaignLimitConfigurationError("campaign limit config is malformed")
    raw_limit = value.get("value")
    if raw_limit is None:
        if self_serve:
            raise CampaignLimitConfigurationError("Self-Serve campaign cap is missing")
        return None
    if isinstance(raw_limit, bool):
        raise CampaignLimitConfigurationError("campaign limit is malformed")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as e:
        raise CampaignLimitConfigurationError("campaign limit is malformed") from e
    if limit < 0 or str(limit) != str(raw_limit).strip():
        raise CampaignLimitConfigurationError("campaign limit is malformed")
    return limit
