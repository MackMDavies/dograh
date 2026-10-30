from unittest.mock import AsyncMock, patch

import pytest

from api.db.campaign_client import (
    ActiveCampaignLimitExceeded,
    _reserve_active_campaign_slot,
)
from api.services.campaign.limits import (
    CampaignLimitConfigurationError,
    get_active_campaign_limit,
)


class _CountResult:
    def __init__(self, count: int):
        self._count = count

    def scalar(self):
        return self._count


@pytest.mark.asyncio
async def test_active_campaign_reservation_locks_then_allows_below_cap():
    session = AsyncMock()
    session.execute.side_effect = [None, _CountResult(2)]

    await _reserve_active_campaign_slot(session, organization_id=42, limit=3)

    assert session.execute.await_count == 2
    lock_query = str(session.execute.await_args_list[0].args[0])
    assert "pg_advisory_xact_lock" in lock_query


@pytest.mark.asyncio
async def test_active_campaign_reservation_rejects_at_cap():
    session = AsyncMock()
    session.execute.side_effect = [None, _CountResult(3)]

    with pytest.raises(ActiveCampaignLimitExceeded) as exc:
        await _reserve_active_campaign_slot(session, organization_id=42, limit=3)

    assert exc.value.limit == 3
    assert exc.value.active == 3


@pytest.mark.asyncio
async def test_scheduled_campaign_excludes_its_own_reserved_slot():
    session = AsyncMock()
    session.execute.side_effect = [None, _CountResult(0)]

    await _reserve_active_campaign_slot(
        session, organization_id=42, limit=1, exclude_campaign_id=101
    )

    count_query = str(session.execute.await_args_list[1].args[0])
    assert "campaigns.id !=" in count_query


@pytest.mark.asyncio
async def test_plan_limit_resolver_reads_zero_as_no_campaigns():
    config = type("Config", (), {"value": {"value": 0}})()
    with patch(
        "api.services.campaign.limits.db_client.get_configuration",
        new_callable=AsyncMock,
        side_effect=[None, config],
    ):
        assert await get_active_campaign_limit(42) == 0


@pytest.mark.asyncio
async def test_self_serve_plan_fails_closed_when_campaign_cap_did_not_sync():
    plan = type("Config", (), {"value": {"value": "subscription_connect"}})()
    with patch(
        "api.services.campaign.limits.db_client.get_configuration",
        new_callable=AsyncMock,
        side_effect=[plan, None],
    ):
        with pytest.raises(CampaignLimitConfigurationError):
            await get_active_campaign_limit(42)


@pytest.mark.asyncio
async def test_self_serve_plan_fails_closed_when_campaign_cap_is_null():
    plan = type("Config", (), {"value": {"value": "subscription_voice"}})()
    config = type("Config", (), {"value": {"value": None}})()
    with patch(
        "api.services.campaign.limits.db_client.get_configuration",
        new_callable=AsyncMock,
        side_effect=[plan, config],
    ):
        with pytest.raises(CampaignLimitConfigurationError):
            await get_active_campaign_limit(42)


@pytest.mark.asyncio
async def test_plan_limit_resolver_fails_closed_on_unreadable_config():
    with patch(
        "api.services.campaign.limits.db_client.get_configuration",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db unavailable"),
    ):
        with pytest.raises(CampaignLimitConfigurationError):
            await get_active_campaign_limit(42)
