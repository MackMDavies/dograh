"""Regression test for the "in_processing" vs "processing" state-string typo
in CampaignOrchestrator._try_complete_immediately.

get_queued_runs_count(states=["queued", "in_processing"]) could never match
anything — "in_processing" isn't a real queued_run_state value (queued /
processing / processed / failed are) — so non_terminal_count was always 0
once no "queued" rows remained, and a campaign could complete/fail while
rows were still stuck in "processing", silently undercounting results.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.campaign.campaign_orchestrator import CampaignOrchestrator


def _campaign(*, total_rows=10, processed_rows=5, failed_rows=0, state="running"):
    campaign = MagicMock()
    campaign.id = 1
    campaign.state = state
    campaign.total_rows = total_rows
    campaign.processed_rows = processed_rows
    campaign.failed_rows = failed_rows
    return campaign


class TestTryCompleteImmediately:
    @pytest.mark.asyncio
    async def test_does_not_complete_while_rows_are_stuck_processing(self):
        """5 processed of 10 total, 0 queued, but 5 still stuck in
        'processing' (e.g. a worker crashed mid-batch) — must NOT complete."""
        campaign = _campaign(total_rows=10, processed_rows=5, failed_rows=0)
        orchestrator = CampaignOrchestrator(redis_client=AsyncMock())

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            # 5 rows genuinely still in "processing" — the real state string.
            mock_db.get_queued_runs_count = AsyncMock(return_value=5)

            await orchestrator._try_complete_immediately(campaign)

            mock_db.get_queued_runs_count.assert_awaited_once_with(
                campaign_id=1, states=["queued", "processing"]
            )
            # Must NOT have completed: falls back to the activity timeout.
            assert 1 in orchestrator._last_activity

    @pytest.mark.asyncio
    async def test_completes_when_no_rows_remain_non_terminal(self):
        campaign = _campaign(total_rows=10, processed_rows=8, failed_rows=2)
        orchestrator = CampaignOrchestrator(redis_client=AsyncMock())

        with (
            patch(
                "api.services.campaign.campaign_orchestrator.db_client"
            ) as mock_db,
            patch.object(
                orchestrator, "_complete_campaign", new=AsyncMock()
            ) as mock_complete,
        ):
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            mock_db.get_queued_runs_count = AsyncMock(return_value=0)

            await orchestrator._try_complete_immediately(campaign)

            mock_complete.assert_awaited_once_with(campaign)
