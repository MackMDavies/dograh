"""Pending work and dispatchable work are different questions.

`_has_pending_work` answers "is anything outstanding at all", including runs
parked for the future — that is what completion checks need, or a campaign
holding a backoff retry would mark itself finished and strand it.

Scheduling a batch needs the narrower question: is there anything the
dispatcher can actually claim *now*? `claim_queued_runs_for_processing`
claims only `queued` rows that are either unscheduled or already due, so a
row parked at 2999-01-01 (a callback awaiting human review) can never be
claimed. Asking the wider question before scheduling made the orchestrator
schedule a batch every 30s that dispatched nothing, forever, and — because
that branch `continue`s — never reach stuck-run recovery or completion.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from api.services.campaign.campaign_orchestrator import CampaignOrchestrator


def _orchestrator():
    return CampaignOrchestrator.__new__(CampaignOrchestrator)


class TestHasDispatchableWork:
    @pytest.mark.asyncio
    async def test_parked_runs_are_not_dispatchable(self):
        """The prod bug: 7 queued rows parked at 2999 are not work to schedule."""
        orch = _orchestrator()

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            # Parked rows are queued-but-not-due: neither claim branch sees them.
            mock_db.get_scheduled_runs_count = AsyncMock(return_value=0)
            mock_db.get_unscheduled_queued_runs_count = AsyncMock(return_value=0)

            assert await orch._has_dispatchable_work(51) is False

    @pytest.mark.asyncio
    async def test_due_retry_is_dispatchable(self):
        orch = _orchestrator()

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            mock_db.get_scheduled_runs_count = AsyncMock(return_value=1)
            mock_db.get_unscheduled_queued_runs_count = AsyncMock(return_value=0)

            assert await orch._has_dispatchable_work(51) is True

    @pytest.mark.asyncio
    async def test_unscheduled_queued_run_is_dispatchable(self):
        orch = _orchestrator()

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            mock_db.get_scheduled_runs_count = AsyncMock(return_value=0)
            mock_db.get_unscheduled_queued_runs_count = AsyncMock(return_value=3)

            assert await orch._has_dispatchable_work(51) is True

    @pytest.mark.asyncio
    async def test_due_check_is_anchored_to_now(self):
        """A run scheduled for the future must not be counted as due."""
        orch = _orchestrator()

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            mock_db.get_scheduled_runs_count = AsyncMock(return_value=0)
            mock_db.get_unscheduled_queued_runs_count = AsyncMock(return_value=0)

            before = datetime.now(UTC)
            await orch._has_dispatchable_work(51)
            after = datetime.now(UTC)

            kwargs = mock_db.get_scheduled_runs_count.await_args.kwargs
            assert before <= kwargs["scheduled_before"] <= after


class TestHasPendingWorkStillCountsFutureWork:
    """The completion path must keep seeing parked/future runs as outstanding."""

    @pytest.mark.asyncio
    async def test_parked_run_still_counts_as_pending(self):
        orch = _orchestrator()

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            mock_db.get_queued_runs_count = AsyncMock(return_value=7)
            mock_db.get_scheduled_runs_count = AsyncMock(return_value=0)

            assert await orch._has_pending_work(51) is True


class TestUnscheduledQueuedRunsCountQuery:
    """The new count must mirror the dispatcher's regular-queue claim branch."""

    def test_filters_to_queued_rows_with_no_scheduled_for(self):
        from sqlalchemy.dialects import postgresql

        from api.db.campaign_client import build_unscheduled_queued_runs_count_query

        sql = str(
            build_unscheduled_queued_runs_count_query(51).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        assert "count" in sql.lower()
        assert "queued_runs.campaign_id = 51" in sql
        assert "queued_runs.state = 'queued'" in sql
        assert "queued_runs.scheduled_for IS NULL" in sql


class TestParkedCampaignDivergence:
    """The whole point: for a parked-only campaign the two answers differ."""

    @pytest.mark.asyncio
    async def test_pending_but_not_dispatchable(self):
        orch = _orchestrator()

        with patch(
            "api.services.campaign.campaign_orchestrator.db_client"
        ) as mock_db:
            # Exactly campaign 51 in prod: 7 queued rows, all parked at 2999.
            mock_db.get_queued_runs_count = AsyncMock(return_value=7)
            mock_db.get_scheduled_runs_count = AsyncMock(return_value=0)
            mock_db.get_unscheduled_queued_runs_count = AsyncMock(return_value=0)

            assert await orch._has_pending_work(51) is True
            assert await orch._has_dispatchable_work(51) is False
