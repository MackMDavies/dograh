"""
Tests for CampaignCallDispatcher.process_batch method.

These tests verify:
1. Basic batch processing functionality
2. Thread-safety via SELECT FOR UPDATE SKIP LOCKED
3. Race condition handling when multiple workers process concurrently
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CampaignModel,
    OrganizationModel,
    QueuedRunModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.campaign.campaign_call_dispatcher import CampaignCallDispatcher
from api.services.campaign.errors import (
    PhoneNumberPoolExhaustedError,
    SuppressedNumberError,
)
from api.services.campaign.rate_limiter import FromNumberLease

# =============================================================================
# Test-specific fixtures
# =============================================================================


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    """
    Create a real session factory for campaign integration tests.

    These tests need real database commits (not savepoints) to test
    concurrent SELECT FOR UPDATE SKIP LOCKED behavior across independent
    connections.

    Patches db_client so CampaignCallDispatcher uses the test database.
    """
    from api.db import db_client

    test_url = setup_test_database
    engine = create_async_engine(test_url, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    yield session_factory

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@dataclass
class CampaignTestData:
    """Container for campaign test data IDs"""

    organization_id: int
    user_id: int
    workflow_id: int
    campaign_id: int
    queued_run_ids: List[int]


@pytest.fixture
async def campaign_test_data(db_session_factory) -> CampaignTestData:
    """
    Create test data for campaign processing tests.

    Creates:
    - Organization
    - User
    - Workflow
    - Campaign (in 'running' state)
    - 10 QueuedRuns (in 'queued' state)
    """
    async with db_session_factory() as session:
        # Create organization
        org = OrganizationModel(
            provider_id=f"test-org-{uuid.uuid4().hex[:8]}",
        )
        session.add(org)
        await session.flush()

        # Create user
        user = UserModel(
            provider_id=f"test-user-{uuid.uuid4().hex[:8]}",
            selected_organization_id=org.id,
        )
        session.add(user)
        await session.flush()

        # Create workflow
        workflow = WorkflowModel(
            name=f"test-workflow-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            organization_id=org.id,
            workflow_definition={
                "nodes": [
                    {
                        "id": "1",
                        "type": "startCall",
                        "position": {"x": 0, "y": 0},
                        "data": {"name": "Start", "prompt": "Hello"},
                    }
                ],
                "edges": [],
            },
            template_context_variables={},
        )
        session.add(workflow)
        await session.flush()

        # Create campaign
        campaign = CampaignModel(
            name=f"test-campaign-{uuid.uuid4().hex[:8]}",
            organization_id=org.id,
            workflow_id=workflow.id,
            created_by=user.id,
            source_type="test",
            source_id="test-source",
            state="running",
            rate_limit_per_second=100,  # High limit to avoid rate limiting in tests
        )
        session.add(campaign)
        await session.flush()

        # Create queued runs
        queued_run_ids = []
        for i in range(10):
            queued_run = QueuedRunModel(
                campaign_id=campaign.id,
                source_uuid=f"test-uuid-{i}",
                context_variables={"phone_number": f"+1555000{i:04d}"},
                state="queued",
            )
            session.add(queued_run)
            await session.flush()
            queued_run_ids.append(queued_run.id)

        await session.commit()

        test_data = CampaignTestData(
            organization_id=org.id,
            user_id=user.id,
            workflow_id=workflow.id,
            campaign_id=campaign.id,
            queued_run_ids=queued_run_ids,
        )

        yield test_data

        # Cleanup
        async with db_session_factory() as cleanup_session:
            # Delete in reverse order of dependencies
            await cleanup_session.execute(
                delete(QueuedRunModel).where(QueuedRunModel.campaign_id == campaign.id)
            )
            await cleanup_session.execute(
                delete(WorkflowRunModel).where(
                    WorkflowRunModel.campaign_id == campaign.id
                )
            )
            await cleanup_session.execute(
                delete(CampaignModel).where(CampaignModel.id == campaign.id)
            )
            await cleanup_session.execute(
                delete(WorkflowModel).where(WorkflowModel.id == workflow.id)
            )
            await cleanup_session.execute(
                delete(UserModel).where(UserModel.id == user.id)
            )
            await cleanup_session.execute(
                delete(OrganizationModel).where(OrganizationModel.id == org.id)
            )
            await cleanup_session.commit()


@pytest.fixture
def mock_dispatch_call():
    """Mock dispatch_call to track which runs were processed."""
    processed_runs = []

    async def mock_dispatch(queued_run, campaign, slot_id):
        # Simulate some processing time
        await asyncio.sleep(0.01)
        processed_runs.append(queued_run.id)
        # Return a mock workflow run
        mock_run = MagicMock()
        mock_run.id = len(processed_runs)
        return mock_run

    return mock_dispatch, processed_runs


@pytest.fixture
def mock_rate_limiter():
    """Mock rate limiter to always allow calls."""

    async def mock_acquire_token(*args, **kwargs):
        return True

    async def mock_try_acquire_slot(*args, **kwargs):
        return f"slot-{uuid.uuid4().hex[:8]}"

    async def mock_release_slot(*args, **kwargs):
        return True

    async def mock_store_mapping(*args, **kwargs):
        pass

    async def mock_get_mapping(*args, **kwargs):
        return None

    async def mock_delete_mapping(*args, **kwargs):
        pass

    async def mock_initialize_from_number_pool(*args, **kwargs):
        return True

    async def mock_acquire_from_number(*args, **kwargs):
        return FromNumberLease(number="+15551234567", lease_id="lease-test")

    async def mock_release_from_number(*args, **kwargs):
        return True

    async def mock_store_from_number_mapping(*args, **kwargs):
        return True

    async def mock_get_from_number_mapping(*args, **kwargs):
        return None

    async def mock_delete_from_number_mapping(*args, **kwargs):
        return True

    return {
        "acquire_token": mock_acquire_token,
        "try_acquire_concurrent_slot": mock_try_acquire_slot,
        "release_concurrent_slot": mock_release_slot,
        "store_workflow_slot_mapping": mock_store_mapping,
        "get_workflow_slot_mapping": mock_get_mapping,
        "delete_workflow_slot_mapping": mock_delete_mapping,
        "initialize_from_number_pool": mock_initialize_from_number_pool,
        "acquire_from_number": mock_acquire_from_number,
        "release_from_number": mock_release_from_number,
        "store_workflow_from_number_mapping": mock_store_from_number_mapping,
        "get_workflow_from_number_mapping": mock_get_from_number_mapping,
        "delete_workflow_from_number_mapping": mock_delete_from_number_mapping,
    }


# =============================================================================
# Tests
# =============================================================================


class TestProcessBatchBasic:
    """Basic tests for process_batch functionality."""

    @pytest.mark.asyncio
    async def test_process_batch_processes_queued_runs(
        self, campaign_test_data, mock_dispatch_call, mock_rate_limiter
    ):
        """Test that process_batch processes queued runs and marks them as processed."""
        mock_dispatch, processed_runs = mock_dispatch_call

        with patch(
            "api.services.campaign.campaign_call_dispatcher.rate_limiter"
        ) as mock_rl:
            # Setup rate limiter mocks
            mock_rl.acquire_token = AsyncMock(
                side_effect=mock_rate_limiter["acquire_token"]
            )
            mock_rl.try_acquire_concurrent_slot = AsyncMock(
                side_effect=mock_rate_limiter["try_acquire_concurrent_slot"]
            )
            mock_rl.release_concurrent_slot = AsyncMock(
                side_effect=mock_rate_limiter["release_concurrent_slot"]
            )
            mock_rl.store_workflow_slot_mapping = AsyncMock(
                side_effect=mock_rate_limiter["store_workflow_slot_mapping"]
            )
            mock_rl.get_workflow_slot_mapping = AsyncMock(
                side_effect=mock_rate_limiter["get_workflow_slot_mapping"]
            )
            mock_rl.delete_workflow_slot_mapping = AsyncMock(
                side_effect=mock_rate_limiter["delete_workflow_slot_mapping"]
            )
            mock_rl.initialize_from_number_pool = AsyncMock(
                side_effect=mock_rate_limiter["initialize_from_number_pool"]
            )
            mock_rl.acquire_from_number = AsyncMock(
                side_effect=mock_rate_limiter["acquire_from_number"]
            )
            mock_rl.release_from_number = AsyncMock(
                side_effect=mock_rate_limiter["release_from_number"]
            )
            mock_rl.store_workflow_from_number_mapping = AsyncMock(
                side_effect=mock_rate_limiter["store_workflow_from_number_mapping"]
            )
            mock_rl.get_workflow_from_number_mapping = AsyncMock(
                side_effect=mock_rate_limiter["get_workflow_from_number_mapping"]
            )
            mock_rl.delete_workflow_from_number_mapping = AsyncMock(
                side_effect=mock_rate_limiter["delete_workflow_from_number_mapping"]
            )

            dispatcher = CampaignCallDispatcher()

            # Mock dispatch_call
            with patch.object(dispatcher, "dispatch_call", side_effect=mock_dispatch):
                # Process batch of 5
                processed_count = await dispatcher.process_batch(
                    campaign_id=campaign_test_data.campaign_id, batch_size=5
                )

            assert processed_count == 5
            assert len(processed_runs) == 5


class TestProcessBatchConcurrency:
    """Tests for concurrent batch processing and database locking."""

    @pytest.mark.asyncio
    async def test_concurrent_process_batch_no_duplicate_processing(
        self,
        campaign_test_data,
        mock_dispatch_call,
        mock_rate_limiter,
        db_session_factory,
    ):
        """
        Test that two concurrent process_batch calls don't process the same runs.

        This verifies the SELECT FOR UPDATE SKIP LOCKED mechanism works correctly.
        """
        mock_dispatch, processed_runs = mock_dispatch_call

        # Reset queued runs to 'queued' state for this test
        async with db_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE queued_runs SET state = 'queued' WHERE campaign_id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        async def run_process_batch():
            """Helper to run process_batch with mocked dependencies."""
            with patch(
                "api.services.campaign.campaign_call_dispatcher.rate_limiter"
            ) as mock_rl:
                mock_rl.acquire_token = AsyncMock(
                    side_effect=mock_rate_limiter["acquire_token"]
                )
                mock_rl.try_acquire_concurrent_slot = AsyncMock(
                    side_effect=mock_rate_limiter["try_acquire_concurrent_slot"]
                )
                mock_rl.release_concurrent_slot = AsyncMock(
                    side_effect=mock_rate_limiter["release_concurrent_slot"]
                )
                mock_rl.store_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["store_workflow_slot_mapping"]
                )
                mock_rl.get_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["get_workflow_slot_mapping"]
                )
                mock_rl.delete_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["delete_workflow_slot_mapping"]
                )
                mock_rl.initialize_from_number_pool = AsyncMock(
                    side_effect=mock_rate_limiter["initialize_from_number_pool"]
                )
                mock_rl.acquire_from_number = AsyncMock(
                    side_effect=mock_rate_limiter["acquire_from_number"]
                )
                mock_rl.release_from_number = AsyncMock(
                    side_effect=mock_rate_limiter["release_from_number"]
                )
                mock_rl.store_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["store_workflow_from_number_mapping"]
                )
                mock_rl.get_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["get_workflow_from_number_mapping"]
                )
                mock_rl.delete_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["delete_workflow_from_number_mapping"]
                )

                dispatcher = CampaignCallDispatcher()

                with patch.object(
                    dispatcher, "dispatch_call", side_effect=mock_dispatch
                ):
                    return await dispatcher.process_batch(
                        campaign_id=campaign_test_data.campaign_id, batch_size=5
                    )

        # Run two process_batch calls concurrently
        results = await asyncio.gather(
            run_process_batch(),
            run_process_batch(),
        )

        # Total processed should be 10 (all queued runs)
        total_processed = sum(results)
        assert total_processed == 10, f"Expected 10 total, got {total_processed}"

        # Each run should be processed exactly once (no duplicates)
        assert len(processed_runs) == 10, f"Expected 10 runs, got {len(processed_runs)}"
        assert len(set(processed_runs)) == 10, "Duplicate runs were processed!"

    @pytest.mark.asyncio
    async def test_concurrent_process_batch_with_different_batch_sizes(
        self,
        campaign_test_data,
        mock_dispatch_call,
        mock_rate_limiter,
        db_session_factory,
    ):
        """
        Test concurrent processing with different batch sizes.

        Worker 1 requests 3 runs, Worker 2 requests 7 runs.
        Total should still be 10 with no duplicates.
        """
        mock_dispatch, processed_runs = mock_dispatch_call

        # Reset queued runs to 'queued' state
        async with db_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE queued_runs SET state = 'queued' WHERE campaign_id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        async def run_process_batch(batch_size: int):
            with patch(
                "api.services.campaign.campaign_call_dispatcher.rate_limiter"
            ) as mock_rl:
                mock_rl.acquire_token = AsyncMock(
                    side_effect=mock_rate_limiter["acquire_token"]
                )
                mock_rl.try_acquire_concurrent_slot = AsyncMock(
                    side_effect=mock_rate_limiter["try_acquire_concurrent_slot"]
                )
                mock_rl.release_concurrent_slot = AsyncMock(
                    side_effect=mock_rate_limiter["release_concurrent_slot"]
                )
                mock_rl.store_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["store_workflow_slot_mapping"]
                )
                mock_rl.get_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["get_workflow_slot_mapping"]
                )
                mock_rl.delete_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["delete_workflow_slot_mapping"]
                )
                mock_rl.initialize_from_number_pool = AsyncMock(
                    side_effect=mock_rate_limiter["initialize_from_number_pool"]
                )
                mock_rl.acquire_from_number = AsyncMock(
                    side_effect=mock_rate_limiter["acquire_from_number"]
                )
                mock_rl.release_from_number = AsyncMock(
                    side_effect=mock_rate_limiter["release_from_number"]
                )
                mock_rl.store_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["store_workflow_from_number_mapping"]
                )
                mock_rl.get_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["get_workflow_from_number_mapping"]
                )
                mock_rl.delete_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["delete_workflow_from_number_mapping"]
                )

                dispatcher = CampaignCallDispatcher()

                with patch.object(
                    dispatcher, "dispatch_call", side_effect=mock_dispatch
                ):
                    return await dispatcher.process_batch(
                        campaign_id=campaign_test_data.campaign_id,
                        batch_size=batch_size,
                    )

        # Run with different batch sizes concurrently
        results = await asyncio.gather(
            run_process_batch(3),
            run_process_batch(7),
        )

        total_processed = sum(results)
        assert total_processed == 10

        # Verify no duplicates
        assert len(set(processed_runs)) == len(processed_runs)

    @pytest.mark.asyncio
    async def test_multiple_concurrent_workers(
        self,
        campaign_test_data,
        mock_dispatch_call,
        mock_rate_limiter,
        db_session_factory,
    ):
        """
        Test with many concurrent workers (simulating production scenario).

        5 workers each requesting 4 runs from a pool of 10.
        Should process all 10 exactly once.
        """
        mock_dispatch, processed_runs = mock_dispatch_call

        # Reset queued runs
        async with db_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE queued_runs SET state = 'queued' WHERE campaign_id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        async def run_process_batch():
            with patch(
                "api.services.campaign.campaign_call_dispatcher.rate_limiter"
            ) as mock_rl:
                mock_rl.acquire_token = AsyncMock(
                    side_effect=mock_rate_limiter["acquire_token"]
                )
                mock_rl.try_acquire_concurrent_slot = AsyncMock(
                    side_effect=mock_rate_limiter["try_acquire_concurrent_slot"]
                )
                mock_rl.release_concurrent_slot = AsyncMock(
                    side_effect=mock_rate_limiter["release_concurrent_slot"]
                )
                mock_rl.store_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["store_workflow_slot_mapping"]
                )
                mock_rl.get_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["get_workflow_slot_mapping"]
                )
                mock_rl.delete_workflow_slot_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["delete_workflow_slot_mapping"]
                )
                mock_rl.initialize_from_number_pool = AsyncMock(
                    side_effect=mock_rate_limiter["initialize_from_number_pool"]
                )
                mock_rl.acquire_from_number = AsyncMock(
                    side_effect=mock_rate_limiter["acquire_from_number"]
                )
                mock_rl.release_from_number = AsyncMock(
                    side_effect=mock_rate_limiter["release_from_number"]
                )
                mock_rl.store_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["store_workflow_from_number_mapping"]
                )
                mock_rl.get_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["get_workflow_from_number_mapping"]
                )
                mock_rl.delete_workflow_from_number_mapping = AsyncMock(
                    side_effect=mock_rate_limiter["delete_workflow_from_number_mapping"]
                )

                dispatcher = CampaignCallDispatcher()

                with patch.object(
                    dispatcher, "dispatch_call", side_effect=mock_dispatch
                ):
                    return await dispatcher.process_batch(
                        campaign_id=campaign_test_data.campaign_id, batch_size=4
                    )

        # Run 5 workers concurrently
        results = await asyncio.gather(*[run_process_batch() for _ in range(5)])

        total_processed = sum(results)
        assert total_processed == 10

        # Verify no duplicates
        assert len(set(processed_runs)) == 10, "Duplicate runs were processed!"

    @pytest.mark.asyncio
    async def test_processing_state_transition(
        self,
        campaign_test_data,
        mock_dispatch_call,
        mock_rate_limiter,
        db_session_factory,
    ):
        """
        Test that runs transition through processing -> processed states correctly.
        """
        mock_dispatch, processed_runs = mock_dispatch_call

        # Reset queued runs
        async with db_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE queued_runs SET state = 'queued' WHERE campaign_id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        with patch(
            "api.services.campaign.campaign_call_dispatcher.rate_limiter"
        ) as mock_rl:
            mock_rl.acquire_token = AsyncMock(
                side_effect=mock_rate_limiter["acquire_token"]
            )
            mock_rl.try_acquire_concurrent_slot = AsyncMock(
                side_effect=mock_rate_limiter["try_acquire_concurrent_slot"]
            )
            mock_rl.release_concurrent_slot = AsyncMock(
                side_effect=mock_rate_limiter["release_concurrent_slot"]
            )
            mock_rl.store_workflow_slot_mapping = AsyncMock(
                side_effect=mock_rate_limiter["store_workflow_slot_mapping"]
            )
            mock_rl.get_workflow_slot_mapping = AsyncMock(
                side_effect=mock_rate_limiter["get_workflow_slot_mapping"]
            )
            mock_rl.delete_workflow_slot_mapping = AsyncMock(
                side_effect=mock_rate_limiter["delete_workflow_slot_mapping"]
            )
            mock_rl.initialize_from_number_pool = AsyncMock(
                side_effect=mock_rate_limiter["initialize_from_number_pool"]
            )
            mock_rl.acquire_from_number = AsyncMock(
                side_effect=mock_rate_limiter["acquire_from_number"]
            )
            mock_rl.release_from_number = AsyncMock(
                side_effect=mock_rate_limiter["release_from_number"]
            )
            mock_rl.store_workflow_from_number_mapping = AsyncMock(
                side_effect=mock_rate_limiter["store_workflow_from_number_mapping"]
            )
            mock_rl.get_workflow_from_number_mapping = AsyncMock(
                side_effect=mock_rate_limiter["get_workflow_from_number_mapping"]
            )
            mock_rl.delete_workflow_from_number_mapping = AsyncMock(
                side_effect=mock_rate_limiter["delete_workflow_from_number_mapping"]
            )

            dispatcher = CampaignCallDispatcher()

            with patch.object(dispatcher, "dispatch_call", side_effect=mock_dispatch):
                await dispatcher.process_batch(
                    campaign_id=campaign_test_data.campaign_id, batch_size=10
                )

        # Verify all runs are in 'processed' state
        async with db_session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT state, COUNT(*) as count FROM queued_runs "
                    "WHERE campaign_id = :campaign_id GROUP BY state"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            states = {row[0]: row[1] for row in result.fetchall()}

        assert states.get("processed", 0) == 10
        assert states.get("queued", 0) == 0
        assert states.get("processing", 0) == 0


class TestProcessBatchCancellation:
    """Cancellation cleanup for claimed queued runs."""

    @pytest.mark.asyncio
    async def test_cancelled_batch_returns_claimed_runs_without_workflows(self):
        dispatcher = CampaignCallDispatcher()
        campaign = MagicMock()
        campaign.id = 42
        campaign.state = "running"
        campaign.organization_id = 7
        campaign.rate_limit_per_second = 1
        campaign.telephony_configuration_id = 170

        queued_runs = [MagicMock(id=101), MagicMock(id=102), MagicMock(id=103)]
        provider = MagicMock()
        provider.from_numbers = []

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.db_client"
            ) as mock_db,
            patch.object(
                dispatcher,
                "get_provider_for_campaign",
                AsyncMock(return_value=provider),
            ),
            patch.object(
                dispatcher,
                "apply_rate_limit",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
        ):
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            mock_db.claim_queued_runs_for_processing = AsyncMock(
                return_value=queued_runs
            )
            mock_db.return_processing_queued_runs_without_workflow = AsyncMock(
                return_value=3
            )

            with pytest.raises(asyncio.CancelledError):
                await dispatcher.process_batch(campaign_id=42, batch_size=3)

            mock_db.return_processing_queued_runs_without_workflow.assert_awaited_once_with(
                [101, 102, 103]
            )


class TestProcessBatchEdgeCases:
    """Edge case tests for process_batch."""

    @pytest.mark.asyncio
    async def test_empty_queue(
        self, campaign_test_data, mock_rate_limiter, db_session_factory
    ):
        """Test process_batch with no queued runs returns 0."""
        # Set all runs to processed
        async with db_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE queued_runs SET state = 'processed' WHERE campaign_id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        with patch(
            "api.services.campaign.campaign_call_dispatcher.rate_limiter"
        ) as mock_rl:
            mock_rl.acquire_token = AsyncMock(
                side_effect=mock_rate_limiter["acquire_token"]
            )
            mock_rl.try_acquire_concurrent_slot = AsyncMock(
                side_effect=mock_rate_limiter["try_acquire_concurrent_slot"]
            )

            dispatcher = CampaignCallDispatcher()
            result = await dispatcher.process_batch(
                campaign_id=campaign_test_data.campaign_id, batch_size=5
            )

        assert result == 0

    @pytest.mark.asyncio
    async def test_campaign_not_running(
        self, campaign_test_data, mock_rate_limiter, db_session_factory
    ):
        """Test process_batch returns 0 if campaign is not in running state."""
        # Set campaign to paused
        async with db_session_factory() as session:
            await session.execute(
                text("UPDATE campaigns SET state = 'paused' WHERE id = :campaign_id"),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        try:
            dispatcher = CampaignCallDispatcher()
            result = await dispatcher.process_batch(
                campaign_id=campaign_test_data.campaign_id, batch_size=5
            )
            assert result == 0
        finally:
            # Restore campaign state
            async with db_session_factory() as session:
                await session.execute(
                    text(
                        "UPDATE campaigns SET state = 'running' WHERE id = :campaign_id"
                    ),
                    {"campaign_id": campaign_test_data.campaign_id},
                )
                await session.commit()

    @pytest.mark.asyncio
    async def test_process_batch_marks_needs_enrichment_as_failed(
        self, campaign_test_data, db_session_factory
    ):
        """A number check_dial_permitted rejects for 'needs_enrichment' must never
        reach dispatch_call, acquire_concurrent_slot, or acquire_from_number — and
        its queued_run must end up 'failed' with the block reason, not stuck
        'processing'. Distinct from the 'suppressed' reason (see the sibling test
        below), which must NOT be counted as a failure."""
        dispatcher = CampaignCallDispatcher()

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.check_dial_permitted",
                new=AsyncMock(return_value=(False, "needs_enrichment", None)),
            ) as mock_check,
            patch.object(
                dispatcher, "dispatch_call", new=AsyncMock()
            ) as mock_dispatch_call,
            patch.object(
                dispatcher, "acquire_concurrent_slot", new=AsyncMock()
            ) as mock_acquire_slot,
        ):
            processed_count = await dispatcher.process_batch(
                campaign_id=campaign_test_data.campaign_id, batch_size=10
            )

        assert processed_count == 0
        mock_check.assert_called()
        mock_dispatch_call.assert_not_called()
        mock_acquire_slot.assert_not_called()

        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT state FROM queued_runs WHERE id = ANY(:ids)"),
                {"ids": campaign_test_data.queued_run_ids},
            )
            states = [row[0] for row in result.fetchall()]
            assert all(s == "failed" for s in states)

    @pytest.mark.asyncio
    async def test_process_batch_marks_suppressed_as_skipped_not_failed(
        self, campaign_test_data, db_session_factory
    ):
        """A number check_dial_permitted rejects for 'suppressed' must route
        through the same skipped_suppressed/suppressed_rows path dispatch_call's
        own suppression check uses, NOT the generic failed/failed_rows path —
        a suppressed contact is dial-time enforcement working as intended, and
        must not be miscounted as a failure (the exact regression fixed in
        campaign_orchestrator.py's completion-state logic elsewhere in this
        feature; this check_dial_permitted path must not reintroduce it)."""
        dispatcher = CampaignCallDispatcher()

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.check_dial_permitted",
                new=AsyncMock(return_value=(False, "suppressed", None)),
            ) as mock_check,
            patch.object(
                dispatcher, "dispatch_call", new=AsyncMock()
            ) as mock_dispatch_call,
            patch.object(
                dispatcher, "acquire_concurrent_slot", new=AsyncMock()
            ) as mock_acquire_slot,
        ):
            processed_count = await dispatcher.process_batch(
                campaign_id=campaign_test_data.campaign_id, batch_size=10
            )

        assert processed_count == 0
        mock_check.assert_called()
        mock_dispatch_call.assert_not_called()
        mock_acquire_slot.assert_not_called()

        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT state FROM queued_runs WHERE id = ANY(:ids)"),
                {"ids": campaign_test_data.queued_run_ids},
            )
            states = [row[0] for row in result.fetchall()]
            assert all(s == "skipped_suppressed" for s in states)

            campaign_row = await session.execute(
                text(
                    "SELECT suppressed_rows, failed_rows FROM campaigns "
                    "WHERE id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            suppressed_rows, failed_rows = campaign_row.fetchone()
            assert suppressed_rows == len(campaign_test_data.queued_run_ids)
            assert failed_rows == 0

    @pytest.mark.asyncio
    async def test_process_batch_defers_a_run_blocked_for_calling_hours(
        self, campaign_test_data, db_session_factory
    ):
        """A run blocked for outside_calling_hours must stay 'queued' with
        scheduled_for pushed to retry_at — never 'failed', never dialed."""
        dispatcher = CampaignCallDispatcher()
        retry_at = "2026-08-09T13:00:00+00:00"

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.check_dial_permitted",
                new=AsyncMock(return_value=(False, "outside_calling_hours", retry_at)),
            ),
            patch.object(dispatcher, "dispatch_call", new=AsyncMock()) as mock_dispatch_call,
            patch.object(dispatcher, "acquire_concurrent_slot", new=AsyncMock()) as mock_acquire_slot,
        ):
            processed_count = await dispatcher.process_batch(
                campaign_id=campaign_test_data.campaign_id, batch_size=10
            )

        assert processed_count == 0
        mock_dispatch_call.assert_not_called()
        mock_acquire_slot.assert_not_called()

        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT state, scheduled_for FROM queued_runs WHERE id = ANY(:ids)"),
                {"ids": campaign_test_data.queued_run_ids},
            )
            rows = result.fetchall()
            assert all(r[0] == "queued" for r in rows)
            assert all(r[1] is not None for r in rows)

    @pytest.mark.asyncio
    async def test_process_batch_passes_calling_hours_off_for_scheduled_callbacks(
        self, campaign_test_data, db_session_factory
    ):
        """A queued run flagged is_scheduled_callback must pass mode='off' to
        check_dial_permitted (bypassing calling-hours) — but must still CALL it,
        since suppression/IVR-blocked checks are not bypassed by a callback
        request, only the calling-hours window is."""
        async with db_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE queued_runs SET context_variables = context_variables || '{\"is_scheduled_callback\": true}'::jsonb "
                    "WHERE campaign_id = :campaign_id"
                ),
                {"campaign_id": campaign_test_data.campaign_id},
            )
            await session.commit()

        dispatcher = CampaignCallDispatcher()

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.check_dial_permitted",
                new=AsyncMock(return_value=(True, "", None)),
            ) as mock_check,
            patch.object(dispatcher, "dispatch_call", new=AsyncMock(side_effect=lambda qr, c, s: MagicMock(id=1))),
            patch.object(dispatcher, "acquire_concurrent_slot", new=AsyncMock(return_value="slot-1")),
            patch.object(dispatcher, "apply_rate_limit", new=AsyncMock()),
        ):
            await dispatcher.process_batch(campaign_id=campaign_test_data.campaign_id, batch_size=10)

        mock_check.assert_called()
        for call in mock_check.await_args_list:
            assert call.args[2] == {"mode": "off"}


class TestDispatchCallSuppression:
    """
    Dial-time suppression enforcement in dispatch_call.

    Mirrors the bare-dispatcher, mocked-module-dependency style used by
    TestProcessBatchCancellation above (no DB fixtures needed) since these
    tests exercise dispatch_call directly rather than process_batch.
    """

    @pytest.mark.asyncio
    async def test_dispatch_call_raises_before_acquiring_from_number_when_suppressed(
        self,
    ):
        """A suppressed number must never reach acquire_from_number."""
        dispatcher = CampaignCallDispatcher()
        campaign = MagicMock()
        campaign.workflow_id = 55
        campaign.organization_id = 7
        campaign.telephony_configuration_id = 170

        queued_run = MagicMock()
        queued_run.id = 101
        queued_run.context_variables = {"phone_number": "+15551234567"}

        workflow = MagicMock()

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.db_client"
            ) as mock_db,
            patch(
                "api.services.campaign.campaign_call_dispatcher.rate_limiter"
            ) as mock_rl,
            patch(
                "api.services.campaign.campaign_call_dispatcher.is_number_suppressed",
                AsyncMock(return_value=True),
            ) as mock_is_suppressed,
            patch.object(
                dispatcher, "get_provider_for_campaign", AsyncMock()
            ) as mock_get_provider,
            patch.object(
                dispatcher, "acquire_from_number", AsyncMock()
            ) as mock_acquire_from_number,
        ):
            mock_db.get_workflow_by_id = AsyncMock(return_value=workflow)
            mock_rl.release_concurrent_slot = AsyncMock(return_value=True)

            with pytest.raises(SuppressedNumberError):
                await dispatcher.dispatch_call(queued_run, campaign, "slot-abc")

            mock_is_suppressed.assert_awaited_once_with(55, "+15551234567")
            # The whole point of the check's placement: never get this far.
            mock_get_provider.assert_not_awaited()
            mock_acquire_from_number.assert_not_awaited()
            # Slot must still be released so a suppressed contact doesn't
            # leak a concurrency slot.
            mock_rl.release_concurrent_slot.assert_awaited_once_with(7, "slot-abc")

    @pytest.mark.asyncio
    async def test_dispatch_call_proceeds_to_acquire_from_number_when_not_suppressed(
        self,
    ):
        """
        Regression guard: a non-suppressed number must still proceed to
        acquire_from_number as before. We stop the flow right at
        acquire_from_number (by having it return None, which dispatch_call
        already handles as pool-exhaustion) rather than mocking the entire
        rest of dispatch_call's call-creation logic.
        """
        dispatcher = CampaignCallDispatcher()
        campaign = MagicMock()
        campaign.workflow_id = 55
        campaign.organization_id = 7
        campaign.telephony_configuration_id = 170

        queued_run = MagicMock()
        queued_run.id = 101
        queued_run.context_variables = {"phone_number": "+15551234567"}

        workflow = MagicMock()
        provider = MagicMock()
        provider.PROVIDER_NAME = "twilio"

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.db_client"
            ) as mock_db,
            patch(
                "api.services.campaign.campaign_call_dispatcher.rate_limiter"
            ) as mock_rl,
            patch(
                "api.services.campaign.campaign_call_dispatcher.is_number_suppressed",
                AsyncMock(return_value=False),
            ) as mock_is_suppressed,
            patch.object(
                dispatcher,
                "get_provider_for_campaign",
                AsyncMock(return_value=provider),
            ),
            patch.object(
                dispatcher, "acquire_from_number", AsyncMock(return_value=None)
            ) as mock_acquire_from_number,
        ):
            mock_db.get_workflow_by_id = AsyncMock(return_value=workflow)
            mock_rl.release_concurrent_slot = AsyncMock(return_value=True)

            with pytest.raises(PhoneNumberPoolExhaustedError):
                await dispatcher.dispatch_call(queued_run, campaign, "slot-abc")

            mock_is_suppressed.assert_awaited_once_with(55, "+15551234567")
            mock_acquire_from_number.assert_awaited_once_with(
                7, telephony_configuration_id=170
            )


class TestProcessBatchSuppressionHandling:
    """
    process_batch's handling of SuppressedNumberError: skip-not-fail.

    Mirrors the bare-dispatcher, mocked-module-dependency style used by
    TestProcessBatchCancellation / TestDispatchCallSuppression above (no DB
    fixtures needed) rather than the db_session_factory integration style
    used by TestProcessBatchBasic/Concurrency/EdgeCases. Those integration
    fixtures require a from-scratch alembic migration run against a local
    Postgres instance, which errors out in this sandbox (pre-existing
    "relation already exists" / pgvector setup gaps unrelated to this
    feature) well before any of this file's tests get a chance to run their
    own assertions. The bare-dispatcher style sidesteps that by never
    touching a real database or running migrations at all.
    """

    @pytest.mark.asyncio
    async def test_suppressed_run_is_skipped_and_batch_continues_to_next_run(
        self,
    ):
        """
        Two queued runs are claimed; dispatch_call raises SuppressedNumberError
        for the first (suppressed) and succeeds normally for the second.

        Asserts:
        - The suppressed run is marked skipped_suppressed (not failed).
        - increment_campaign_suppressed_rows is called for the campaign.
        - append_campaign_log records a call_skipped_suppressed event.
        - Critically, the loop does NOT abort: dispatch_call is still invoked
          for the second run, and it is counted as processed. This is the
          "skip not fail" behavior the commit title promises.
        """
        dispatcher = CampaignCallDispatcher()
        campaign = MagicMock()
        campaign.id = 42
        campaign.state = "running"
        campaign.organization_id = 7
        campaign.workflow_id = 55
        campaign.rate_limit_per_second = 100
        campaign.telephony_configuration_id = 170

        suppressed_run = MagicMock()
        suppressed_run.id = 201
        suppressed_run.context_variables = {"phone_number": "+15550001111"}

        ok_run = MagicMock()
        ok_run.id = 202
        ok_run.context_variables = {"phone_number": "+15552223333"}

        queued_runs = [suppressed_run, ok_run]

        provider = MagicMock()
        provider.from_numbers = []

        dispatch_call_ids: list[int] = []

        async def fake_dispatch_call(queued_run, campaign, slot_id):
            dispatch_call_ids.append(queued_run.id)
            if queued_run.id == suppressed_run.id:
                raise SuppressedNumberError(
                    campaign.workflow_id, queued_run.context_variables["phone_number"]
                )
            workflow_run = MagicMock()
            workflow_run.id = 999
            return workflow_run

        with (
            patch(
                "api.services.campaign.campaign_call_dispatcher.db_client"
            ) as mock_db,
            patch(
                "api.services.workflow_active_check.check_workflow_active",
                AsyncMock(return_value=(True, None)),
            ),
            patch(
                "api.services.wallet_check.check_wallet_before_call",
                AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                dispatcher,
                "get_provider_for_campaign",
                AsyncMock(return_value=provider),
            ),
            patch.object(
                dispatcher, "apply_rate_limit", AsyncMock(return_value=None)
            ),
            patch.object(
                dispatcher,
                "acquire_concurrent_slot",
                AsyncMock(return_value="slot-abc"),
            ),
            patch.object(
                dispatcher, "dispatch_call", side_effect=fake_dispatch_call
            ) as mock_dispatch_call,
        ):
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            mock_db.claim_queued_runs_for_processing = AsyncMock(
                return_value=queued_runs
            )
            mock_db.update_queued_run = AsyncMock()
            mock_db.increment_campaign_processed_rows = AsyncMock()
            mock_db.increment_campaign_suppressed_rows = AsyncMock()
            mock_db.append_campaign_log = AsyncMock()

            processed_count = await dispatcher.process_batch(
                campaign_id=42, batch_size=2
            )

        # The batch did not abort: dispatch_call was invoked for BOTH runs,
        # in order, proving the loop continued past the suppressed run.
        assert dispatch_call_ids == [201, 202]
        assert mock_dispatch_call.await_count == 2

        # Only the non-suppressed run counts as processed.
        assert processed_count == 1

        # The suppressed run was marked skipped_suppressed, not failed.
        skipped_calls = [
            call
            for call in mock_db.update_queued_run.await_args_list
            if call.kwargs.get("queued_run_id") == 201
        ]
        assert len(skipped_calls) == 1
        assert skipped_calls[0].kwargs["state"] == "skipped_suppressed"

        # The successful run was marked processed as usual.
        processed_calls = [
            call
            for call in mock_db.update_queued_run.await_args_list
            if call.kwargs.get("queued_run_id") == 202
        ]
        assert len(processed_calls) == 1
        assert processed_calls[0].kwargs["state"] == "processed"

        mock_db.increment_campaign_suppressed_rows.assert_awaited_once_with(42)
        mock_db.increment_campaign_processed_rows.assert_awaited_once_with(42)

        mock_db.append_campaign_log.assert_awaited_once()
        log_call = mock_db.append_campaign_log.await_args
        assert log_call.kwargs["event"] == "call_skipped_suppressed"
        assert log_call.kwargs["campaign_id"] == 42
        assert log_call.kwargs["details"]["queued_run_id"] == 201
