"""
Tests for CampaignRunnerService._count_failed_campaign_calls.

Guards against regressing to the old get_workflow_runs_by_campaign call
(full-row fetch, no limit) — this must go through the lean
get_campaign_run_logs path instead, since the frontend polls
/campaign/{id}/progress continuously while a campaign is active.
"""

from unittest.mock import AsyncMock, patch

import pytest

from api.services.campaign.runner import CampaignRunnerService


class TestCountFailedCampaignCalls:
    @pytest.mark.asyncio
    async def test_counts_runs_with_terminal_failure_statuses(self):
        run_logs = [
            {"telephony_status_callbacks": [{"status": "completed"}]},
            {"telephony_status_callbacks": [{"status": "failed"}]},
            {"telephony_status_callbacks": [{"status": "ringing"}, {"status": "busy"}]},
            {"telephony_status_callbacks": [{"status": "no-answer"}]},
            {},  # no callbacks at all yet
        ]
        with patch("api.services.campaign.runner.db_client") as mock_db:
            mock_db.get_campaign_run_logs = AsyncMock(return_value=run_logs)

            service = CampaignRunnerService()
            count = await service._count_failed_campaign_calls(campaign_id=42)

            mock_db.get_campaign_run_logs.assert_awaited_once_with(42)
            assert count == 3

    @pytest.mark.asyncio
    async def test_no_runs_returns_zero(self):
        with patch("api.services.campaign.runner.db_client") as mock_db:
            mock_db.get_campaign_run_logs = AsyncMock(return_value=[])

            service = CampaignRunnerService()
            count = await service._count_failed_campaign_calls(campaign_id=42)

            assert count == 0
