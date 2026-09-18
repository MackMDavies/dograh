"""Regression test: a queued run claimed for processing that never became a call.

`claim_queued_runs_for_processing` flips a row to `processing` BEFORE the
dispatcher writes its workflow run. A failure in that gap — worker restart, a
telephony error before the run row exists — strands the row in `processing`
with no workflow run attached.

`_recover_stuck_runs` could not see those. It asks
`get_stuck_campaign_runs` for INCOMPLETE WORKFLOW RUNS, and an abandoned claim
has no workflow run at all, so the sweep found nothing and reported the campaign
healthy. Measured on prod campaign 51 ("Sam — Callbacks") on 2026-09-18:

    queued_runs 404 and 916    state=processing since 2026-09-17
    workflow_runs for them     0
    incomplete workflow_runs   0      <- what recovery looks at

916 was a prospect who asked to be rung back within the hour. The call was never
placed, and nothing in the system would ever have freed it.
"""

import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Same stub as test_campaign_orchestrator_completion: importing the orchestrator
# drags in the arq/pipecat stack, which need not be installed to exercise this
# logic.
try:
    import api.tasks.arq  # noqa: F401
except (ModuleNotFoundError, ValueError):
    sys.modules["api.tasks.arq"] = MagicMock()

from api.services.campaign.campaign_orchestrator import CampaignOrchestrator


def _abandoned(run_id, *, retry_count=0, hours_ago=20):
    run = MagicMock()
    run.id = run_id
    run.retry_count = retry_count
    run.scheduled_for = datetime.now(UTC) - timedelta(hours=hours_ago)
    run.created_at = run.scheduled_for - timedelta(hours=1)
    return run


def _campaign(max_retries=2):
    c = MagicMock()
    c.id = 51
    c.retry_config = {"max_retries": max_retries}
    return c


@pytest.mark.asyncio
async def test_abandoned_claim_is_requeued_not_failed():
    """Nothing was dialled, so the work is still outstanding."""
    orch = CampaignOrchestrator(redis_client=AsyncMock())
    with patch("api.services.campaign.campaign_orchestrator.db_client") as db:
        db.get_abandoned_queued_runs = AsyncMock(return_value=[_abandoned(916)])
        db.get_campaign_by_id = AsyncMock(return_value=_campaign())
        db.update_queued_run = AsyncMock()

        await orch._recover_abandoned_claims(51)

        db.update_queued_run.assert_awaited_once_with(916, state="queued", retry_count=1)


@pytest.mark.asyncio
async def test_repeated_abandonment_is_bounded_by_the_retry_ceiling():
    """Requeueing forever would be its own silent failure."""
    orch = CampaignOrchestrator(redis_client=AsyncMock())
    with patch("api.services.campaign.campaign_orchestrator.db_client") as db:
        db.get_abandoned_queued_runs = AsyncMock(return_value=[_abandoned(404, retry_count=2)])
        db.get_campaign_by_id = AsyncMock(return_value=_campaign(max_retries=2))
        db.update_queued_run = AsyncMock()

        await orch._recover_abandoned_claims(51)

        # Failed, so it stops being re-claimed and is visible as a failure
        # rather than quietly vanishing.
        db.update_queued_run.assert_awaited_once_with(404, state="failed")


@pytest.mark.asyncio
async def test_nothing_abandoned_touches_nothing():
    orch = CampaignOrchestrator(redis_client=AsyncMock())
    with patch("api.services.campaign.campaign_orchestrator.db_client") as db:
        db.get_abandoned_queued_runs = AsyncMock(return_value=[])
        db.get_campaign_by_id = AsyncMock()
        db.update_queued_run = AsyncMock()

        await orch._recover_abandoned_claims(51)

        db.update_queued_run.assert_not_awaited()
        # Not even looked up — no work, no queries.
        db.get_campaign_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lookup_failure_never_breaks_the_orchestrator_loop():
    """This runs inside the per-campaign loop; one bad query must not stop it."""
    orch = CampaignOrchestrator(redis_client=AsyncMock())
    with patch("api.services.campaign.campaign_orchestrator.db_client") as db:
        db.get_abandoned_queued_runs = AsyncMock(side_effect=RuntimeError("db down"))
        db.update_queued_run = AsyncMock()

        await orch._recover_abandoned_claims(51)  # must not raise

        db.update_queued_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_stuck_runs_also_sweeps_abandoned_claims():
    """The two recoveries are separate questions and both must be asked.

    Wiring regression: _recover_stuck_runs is what the orchestrator loop calls,
    so an abandoned-claim sweep that is never reached from there is inert — the
    same shape as the bug it fixes.
    """
    orch = CampaignOrchestrator(redis_client=AsyncMock())
    with patch("api.services.campaign.campaign_orchestrator.db_client") as db:
        db.get_stuck_campaign_runs = AsyncMock(return_value=[])
        db.get_abandoned_queued_runs = AsyncMock(return_value=[])
        db.get_campaign_by_id = AsyncMock()
        db.update_queued_run = AsyncMock()

        await orch._recover_stuck_runs(51)

        db.get_abandoned_queued_runs.assert_awaited_once()
