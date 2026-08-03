"""Regression tests for two tenant-isolation gaps in PUT /campaign/{id}:

1. workflow_id was written with zero ownership check (unlike create_campaign,
   which validates it) — a client could repoint a campaign at another org's
   workflow/agent.
2. telephony_configuration_id fell back to an UNSCOPED lookup when the
   caller's own org didn't have it — letting a client point a campaign at
   any other org's telephony config (including its encrypted credentials).
   The fix narrows that fallback to the platform org specifically (the
   already-established get_platform_organization_id() pattern used
   elsewhere for legitimately shared platform resources).
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routes.campaign import UpdateCampaignRequest, update_campaign


def _user(*, is_superuser=False, org_id=3, user_id=10) -> SimpleNamespace:
    return SimpleNamespace(is_superuser=is_superuser, selected_organization_id=org_id, id=user_id)


def _campaign(*, org_id=3, workflow_id=100, telephony_configuration_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=41,
        name="Test Campaign",
        organization_id=org_id,
        workflow_id=workflow_id,
        state="failed",
        source_type="csv",
        source_id="abc",
        total_rows=10,
        processed_rows=0,
        failed_rows=0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
        completed_at=None,
        retry_config=None,
        orchestrator_metadata={},
        telephony_configuration_id=telephony_configuration_id,
        logs=[],
    )


class TestWorkflowIdOwnershipValidation:
    @pytest.mark.asyncio
    async def test_non_superuser_cannot_repoint_to_another_orgs_workflow(self):
        campaign = _campaign(org_id=3)
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.get_campaign = AsyncMock(return_value=campaign)
            mock_db.get_workflow_name = AsyncMock(return_value=None)  # not found in caller's org
            mock_db.update_campaign = AsyncMock()

            with pytest.raises(HTTPException) as exc_info:
                await update_campaign(
                    41,
                    UpdateCampaignRequest(workflow_id=999),
                    user=_user(is_superuser=False, org_id=3),
                )
            assert exc_info.value.status_code == 404
            mock_db.get_workflow_name.assert_awaited_once_with(999, organization_id=3)
            mock_db.update_campaign.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_superuser_can_repoint_to_own_orgs_workflow(self):
        campaign = _campaign(org_id=3)
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.get_campaign = AsyncMock(return_value=campaign)
            mock_db.get_workflow_name = AsyncMock(return_value="My Agent")
            mock_db.update_campaign = AsyncMock()
            mock_db.get_queued_runs_stats_for_campaigns = AsyncMock(return_value={})

            await update_campaign(
                41,
                UpdateCampaignRequest(workflow_id=101),
                user=_user(is_superuser=False, org_id=3),
            )

            mock_db.update_campaign.assert_awaited_once()
            assert mock_db.update_campaign.call_args.kwargs["workflow_id"] == 101

    @pytest.mark.asyncio
    async def test_superuser_bypasses_workflow_ownership_check(self):
        campaign = _campaign(org_id=3)
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.get_campaign = AsyncMock(return_value=campaign)
            mock_db.update_campaign = AsyncMock()
            mock_db.get_queued_runs_stats_for_campaigns = AsyncMock(return_value={})
            mock_db.get_workflow_name = AsyncMock(return_value="Some Agent")

            await update_campaign(
                41,
                UpdateCampaignRequest(workflow_id=999),
                user=_user(is_superuser=True, org_id=1),
            )

            # get_workflow_name is still called once at the end for the
            # response's workflow_name field (using the ORIGINAL workflow_id,
            # 100) — what matters is the ownership-check call for 999 never
            # happens for a superuser.
            for call in mock_db.get_workflow_name.await_args_list:
                assert call.args[0] != 999
            mock_db.update_campaign.assert_awaited_once()
            assert mock_db.update_campaign.call_args.kwargs["workflow_id"] == 999


class TestTelephonyConfigurationCrossOrgFallback:
    @pytest.mark.asyncio
    async def test_non_superuser_cannot_reference_an_unrelated_orgs_config(self):
        """The old unscoped fallback let this succeed against ANY org's config."""
        campaign = _campaign(org_id=3)
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.get_campaign = AsyncMock(return_value=campaign)
            mock_db.get_platform_organization_id = AsyncMock(return_value=1)
            # Both the caller's own org AND the platform-org-scoped lookup
            # find nothing — config belongs to some unrelated third org.
            mock_db.get_telephony_configuration_for_org = AsyncMock(
                side_effect=[None, None]
            )
            mock_db.update_campaign = AsyncMock()

            with pytest.raises(HTTPException) as exc_info:
                await update_campaign(
                    41,
                    UpdateCampaignRequest(telephony_configuration_id=55),
                    user=_user(is_superuser=False, org_id=3),
                )
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == "telephony_configuration_not_found"
            mock_db.get_telephony_configuration.assert_not_called()  # never falls back unscoped
            mock_db.update_campaign.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_superuser_can_reference_the_platform_orgs_shared_config(self):
        """Preserves the legitimate "Sysevo Managed" cross-org use case."""
        campaign = _campaign(org_id=3)
        platform_cfg = SimpleNamespace(id=2, organization_id=1, name="Sysevo Managed")
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.get_campaign = AsyncMock(return_value=campaign)
            mock_db.get_platform_organization_id = AsyncMock(return_value=1)
            mock_db.get_telephony_configuration_for_org = AsyncMock(
                side_effect=[None, platform_cfg]
            )
            mock_db.update_campaign = AsyncMock()
            mock_db.get_queued_runs_stats_for_campaigns = AsyncMock(return_value={})
            mock_db.get_workflow_name = AsyncMock(return_value="Some Agent")

            await update_campaign(
                41,
                UpdateCampaignRequest(telephony_configuration_id=2),
                user=_user(is_superuser=False, org_id=3),
            )

            mock_db.get_telephony_configuration.assert_not_called()
            assert mock_db.get_telephony_configuration_for_org.await_args_list == [
                ((2, 3),),
                ((2, 1),),
            ]
            mock_db.update_campaign.assert_awaited_once()
            assert mock_db.update_campaign.call_args.kwargs["telephony_configuration_id"] == 2

    @pytest.mark.asyncio
    async def test_superuser_bypasses_org_scoping_entirely(self):
        campaign = _campaign(org_id=3)
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.get_campaign = AsyncMock(return_value=campaign)
            mock_db.get_telephony_configuration = AsyncMock(
                return_value=SimpleNamespace(id=55, organization_id=9, name="Someone Else's")
            )
            mock_db.update_campaign = AsyncMock()
            mock_db.get_queued_runs_stats_for_campaigns = AsyncMock(return_value={})
            mock_db.get_workflow_name = AsyncMock(return_value="Some Agent")

            await update_campaign(
                41,
                UpdateCampaignRequest(telephony_configuration_id=55),
                user=_user(is_superuser=True, org_id=1),
            )

            mock_db.get_telephony_configuration_for_org.assert_not_called()
            mock_db.update_campaign.assert_awaited_once()
