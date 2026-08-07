"""
Tests for scheduled campaign launches: shared start validation, the
race-safe claim, and the orchestrator's due-scheduled scan.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestValidateCampaignStartable:
    @pytest.mark.asyncio
    async def test_raises_when_no_telephony_config(self):
        from api.services.campaign.runner import CampaignValidationError, validate_campaign_startable

        campaign = MagicMock(telephony_configuration_id=None, organization_id=1, workflow_id=5)
        user = MagicMock()

        with patch("api.services.campaign.runner.db_client") as mock_db:
            mock_db.list_telephony_configurations = AsyncMock(return_value=[])
            with pytest.raises(CampaignValidationError) as exc_info:
                await validate_campaign_startable(campaign, user)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_when_quota_exhausted(self):
        from api.services.campaign.runner import CampaignValidationError, validate_campaign_startable

        campaign = MagicMock(telephony_configuration_id=42, organization_id=1, workflow_id=5)
        user = MagicMock()
        quota_result = MagicMock(has_quota=False, error_message="quota exceeded")

        with patch("api.services.campaign.runner.check_dograh_quota", AsyncMock(return_value=quota_result)):
            with pytest.raises(CampaignValidationError) as exc_info:
                await validate_campaign_startable(campaign, user)
            assert exc_info.value.status_code == 402

    @pytest.mark.asyncio
    async def test_passes_when_config_and_quota_ok(self):
        from api.services.campaign.runner import validate_campaign_startable

        campaign = MagicMock(telephony_configuration_id=42, organization_id=1, workflow_id=5)
        user = MagicMock()
        quota_result = MagicMock(has_quota=True)

        with patch("api.services.campaign.runner.check_dograh_quota", AsyncMock(return_value=quota_result)):
            await validate_campaign_startable(campaign, user)  # must not raise


class TestFireScheduledCampaign:
    @pytest.mark.asyncio
    async def test_fires_when_claim_succeeds(self):
        from api.services.campaign.runner import CampaignRunnerService

        with patch("api.services.campaign.runner.db_client") as mock_db, \
             patch("api.services.campaign.runner.enqueue_job", AsyncMock()) as mock_enqueue:
            mock_db.claim_scheduled_campaign = AsyncMock(return_value=True)

            result = await CampaignRunnerService().fire_scheduled_campaign(99)

            assert result is True
            mock_enqueue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_claim_lost_to_race(self):
        from api.services.campaign.runner import CampaignRunnerService

        with patch("api.services.campaign.runner.db_client") as mock_db, \
             patch("api.services.campaign.runner.enqueue_job", AsyncMock()) as mock_enqueue:
            mock_db.claim_scheduled_campaign = AsyncMock(return_value=False)

            result = await CampaignRunnerService().fire_scheduled_campaign(99)

            assert result is False
            mock_enqueue.assert_not_awaited()


class TestCheckDueScheduledCampaigns:
    @pytest.mark.asyncio
    async def test_fires_each_due_campaign(self):
        from api.services.campaign.campaign_orchestrator import CampaignOrchestrator

        orchestrator = CampaignOrchestrator(redis_client=MagicMock())
        due_campaign = MagicMock(id=7, created_by=42)
        user = MagicMock()

        with patch("api.services.campaign.campaign_orchestrator.db_client") as mock_db, \
             patch(
                 "api.services.campaign.campaign_orchestrator.campaign_runner_service"
             ) as mock_runner, \
             patch(
                 "api.services.campaign.campaign_orchestrator.validate_campaign_startable",
                 AsyncMock(),
             ) as mock_validate, \
             patch(
                 "api.services.campaign.campaign_orchestrator.notify_campaign_scheduled_started",
                 AsyncMock(),
             ) as mock_notify:
            mock_db.get_due_scheduled_campaigns = AsyncMock(return_value=[due_campaign])
            mock_db.get_user_by_id = AsyncMock(return_value=user)
            mock_runner.fire_scheduled_campaign = AsyncMock(return_value=True)

            await orchestrator._check_due_scheduled_campaigns()

            mock_validate.assert_awaited_once_with(due_campaign, user)
            mock_runner.fire_scheduled_campaign.assert_awaited_once_with(7)
            mock_notify.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_no_notify_when_fire_returns_false(self):
        from api.services.campaign.campaign_orchestrator import CampaignOrchestrator

        orchestrator = CampaignOrchestrator(redis_client=MagicMock())
        due_campaign = MagicMock(id=7, created_by=42)
        user = MagicMock()

        with patch("api.services.campaign.campaign_orchestrator.db_client") as mock_db, \
             patch(
                 "api.services.campaign.campaign_orchestrator.campaign_runner_service"
             ) as mock_runner, \
             patch(
                 "api.services.campaign.campaign_orchestrator.validate_campaign_startable",
                 AsyncMock(),
             ), \
             patch(
                 "api.services.campaign.campaign_orchestrator.notify_campaign_scheduled_started",
                 AsyncMock(),
             ) as mock_notify:
            mock_db.get_due_scheduled_campaigns = AsyncMock(return_value=[due_campaign])
            mock_db.get_user_by_id = AsyncMock(return_value=user)
            mock_runner.fire_scheduled_campaign = AsyncMock(return_value=False)

            await orchestrator._check_due_scheduled_campaigns()

            mock_notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_firing_when_validation_fails(self):
        from api.services.campaign.campaign_orchestrator import CampaignOrchestrator
        from api.services.campaign.runner import CampaignValidationError

        orchestrator = CampaignOrchestrator(redis_client=MagicMock())
        due_campaign = MagicMock(id=7, created_by=42)
        user = MagicMock()

        with patch("api.services.campaign.campaign_orchestrator.db_client") as mock_db, \
             patch(
                 "api.services.campaign.campaign_orchestrator.campaign_runner_service"
             ) as mock_runner, \
             patch(
                 "api.services.campaign.campaign_orchestrator.validate_campaign_startable",
                 AsyncMock(side_effect=CampaignValidationError(401, "no telephony config")),
             ):
            mock_db.get_due_scheduled_campaigns = AsyncMock(return_value=[due_campaign])
            mock_db.get_user_by_id = AsyncMock(return_value=user)
            mock_runner.fire_scheduled_campaign = AsyncMock(return_value=True)

            await orchestrator._check_due_scheduled_campaigns()  # must not raise

            mock_runner.fire_scheduled_campaign.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_firing_when_creator_user_not_found(self):
        from api.services.campaign.campaign_orchestrator import CampaignOrchestrator

        orchestrator = CampaignOrchestrator(redis_client=MagicMock())
        due_campaign = MagicMock(id=7, created_by=42)

        with patch("api.services.campaign.campaign_orchestrator.db_client") as mock_db, \
             patch(
                 "api.services.campaign.campaign_orchestrator.campaign_runner_service"
             ) as mock_runner:
            mock_db.get_due_scheduled_campaigns = AsyncMock(return_value=[due_campaign])
            mock_db.get_user_by_id = AsyncMock(return_value=None)
            mock_runner.fire_scheduled_campaign = AsyncMock(return_value=True)

            await orchestrator._check_due_scheduled_campaigns()  # must not raise

            mock_runner.fire_scheduled_campaign.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_the_rest(self):
        from api.services.campaign.campaign_orchestrator import CampaignOrchestrator

        orchestrator = CampaignOrchestrator(redis_client=MagicMock())
        campaign_a = MagicMock(id=1, created_by=1)
        campaign_b = MagicMock(id=2, created_by=2)
        user = MagicMock()

        async def fire_side_effect(campaign_id):
            if campaign_id == 1:
                raise RuntimeError("boom")
            return True

        with patch("api.services.campaign.campaign_orchestrator.db_client") as mock_db, \
             patch(
                 "api.services.campaign.campaign_orchestrator.campaign_runner_service"
             ) as mock_runner, \
             patch(
                 "api.services.campaign.campaign_orchestrator.validate_campaign_startable",
                 AsyncMock(),
             ):
            mock_db.get_due_scheduled_campaigns = AsyncMock(return_value=[campaign_a, campaign_b])
            mock_db.get_user_by_id = AsyncMock(return_value=user)
            mock_runner.fire_scheduled_campaign = AsyncMock(side_effect=fire_side_effect)

            await orchestrator._check_due_scheduled_campaigns()  # must not raise

            assert mock_runner.fire_scheduled_campaign.await_count == 2
