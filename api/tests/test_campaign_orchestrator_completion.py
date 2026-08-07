"""Regression test for the "in_processing" vs "processing" state-string typo
in CampaignOrchestrator._try_complete_immediately.

get_queued_runs_count(states=["queued", "in_processing"]) could never match
anything — "in_processing" isn't a real queued_run_state value (queued /
processing / processed / failed are) — so non_terminal_count was always 0
once no "queued" rows remained, and a campaign could complete/fail while
rows were still stuck in "processing", silently undercounting results.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# api.services.campaign.campaign_orchestrator imports api.tasks.arq at module
# load time, which in turn drags in the full pipecat pipeline/tracing stack.
# In sandboxes where that optional stack isn't installed, stub it out so this
# file can still exercise the orchestrator's own (pipecat-independent) logic;
# where the real package is present, this is a no-op.
try:
    import api.tasks.arq  # noqa: F401
except ModuleNotFoundError:
    sys.modules["api.tasks.arq"] = MagicMock()

from api.services.campaign.campaign_orchestrator import CampaignOrchestrator


def _campaign(*, total_rows=10, processed_rows=5, failed_rows=0, state="running"):
    campaign = MagicMock()
    campaign.id = 1
    campaign.state = state
    campaign.total_rows = total_rows
    campaign.processed_rows = processed_rows
    campaign.failed_rows = failed_rows
    return campaign


def _campaign_for_completion(
    *,
    total_rows=10,
    processed_rows=5,
    failed_rows=0,
    suppressed_rows=0,
    is_standing=False,
    started_at=None,
):
    campaign = MagicMock()
    campaign.id = 1
    campaign.total_rows = total_rows
    campaign.processed_rows = processed_rows
    campaign.failed_rows = failed_rows
    campaign.suppressed_rows = suppressed_rows
    campaign.is_standing = is_standing
    campaign.started_at = started_at
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


class TestCompleteCampaignFinalState:
    """Regression test: a campaign where every contact was correctly skipped
    for suppression must resolve to "completed", not "failed". Before the
    fix, `final_state` only excused `processed == 0` when nothing at all
    happened — it didn't know about `suppressed_rows`, so a 100%-suppressed
    campaign (the suppression feature working exactly as designed) was
    mislabeled a failure.
    """

    @pytest.mark.asyncio
    async def test_fully_suppressed_campaign_completes_not_fails(self):
        campaign = _campaign_for_completion(
            total_rows=5,
            processed_rows=0,
            failed_rows=0,
            suppressed_rows=5,
        )
        orchestrator = CampaignOrchestrator(redis_client=AsyncMock())

        with (
            patch(
                "api.services.campaign.campaign_orchestrator.db_client"
            ) as mock_db,
            patch.object(
                orchestrator, "_has_pending_work", new=AsyncMock(return_value=False)
            ),
        ):
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            mock_db.update_campaign = AsyncMock()
            orchestrator.publisher.publish_campaign_completed = AsyncMock()

            await orchestrator._complete_campaign(campaign)

            mock_db.update_campaign.assert_awaited_once()
            _, kwargs = mock_db.update_campaign.call_args
            assert kwargs["state"] == "completed"

            orchestrator.publisher.publish_campaign_completed.assert_awaited_once()
            _, publish_kwargs = (
                orchestrator.publisher.publish_campaign_completed.call_args
            )
            assert publish_kwargs["suppressed_rows"] == 5

    @pytest.mark.asyncio
    async def test_genuinely_untouched_campaign_still_fails(self):
        """Sanity check the fix didn't loosen the original failure case:
        nothing processed, nothing failed, nothing suppressed => still
        "failed" (e.g. every call errored before any counter was touched)."""
        campaign = _campaign_for_completion(
            total_rows=5,
            processed_rows=0,
            failed_rows=0,
            suppressed_rows=0,
        )
        orchestrator = CampaignOrchestrator(redis_client=AsyncMock())

        with (
            patch(
                "api.services.campaign.campaign_orchestrator.db_client"
            ) as mock_db,
            patch.object(
                orchestrator, "_has_pending_work", new=AsyncMock(return_value=False)
            ),
        ):
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            mock_db.update_campaign = AsyncMock()
            orchestrator.publisher.publish_campaign_completed = AsyncMock()

            await orchestrator._complete_campaign(campaign)

            _, kwargs = mock_db.update_campaign.call_args
            assert kwargs["state"] == "failed"
