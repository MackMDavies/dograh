"""Regression tests: a redelivered terminal telephony status webhook must
not re-run side effects.

Telephony providers commonly redeliver a status callback on timeout/non-2xx
response. Before this fix, only the final DB write was guarded against
redelivery — release_call_slot, circuit_breaker.record_and_evaluate, and
(for busy/no-answer) publish_retry_needed all re-ran unconditionally. Since
campaign_orchestrator._schedule_retry creates a NEW child queued_run every
time it's invoked, a redelivered "busy"/"no-answer" webhook meant a
duplicate dial of the same number.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)


def _workflow_run(*, state="initialized", campaign_id=7, queued_run_id=99):
    run = MagicMock()
    run.id = 1
    run.state = state
    run.campaign_id = campaign_id
    run.queued_run_id = queued_run_id
    run.logs = {}
    run.gathered_context = {}
    return run


class TestFailureBranchIdempotency:
    @pytest.mark.asyncio
    async def test_first_delivery_publishes_retry_and_releases_slot(self):
        workflow_run = _workflow_run(state="initialized")
        with (
            patch("api.services.telephony.status_processor.db_client") as mock_db,
            patch(
                "api.services.telephony.status_processor.campaign_call_dispatcher"
            ) as mock_dispatcher,
            patch(
                "api.services.telephony.status_processor.circuit_breaker"
            ) as mock_cb,
            patch(
                "api.services.telephony.status_processor.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
            mock_db.update_workflow_run = AsyncMock()
            mock_dispatcher.release_call_slot = AsyncMock()
            mock_cb.record_and_evaluate = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await _process_status_update(
                1, StatusCallbackRequest(call_id="c1", status="busy")
            )

            mock_dispatcher.release_call_slot.assert_awaited_once_with(1)
            mock_pub.publish_retry_needed.assert_awaited_once_with(
                workflow_run_id=1, reason="busy", campaign_id=7, queued_run_id=99
            )

    @pytest.mark.asyncio
    async def test_redelivered_failure_does_not_republish_retry(self):
        """Run is ALREADY terminal (a prior delivery already processed
        it) — a redelivered webhook must be a no-op for side effects."""
        workflow_run = _workflow_run(state="completed")
        with (
            patch("api.services.telephony.status_processor.db_client") as mock_db,
            patch(
                "api.services.telephony.status_processor.campaign_call_dispatcher"
            ) as mock_dispatcher,
            patch(
                "api.services.telephony.status_processor.circuit_breaker"
            ) as mock_cb,
            patch(
                "api.services.telephony.status_processor.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
            mock_db.update_workflow_run = AsyncMock()
            mock_dispatcher.release_call_slot = AsyncMock()
            mock_cb.record_and_evaluate = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await _process_status_update(
                1, StatusCallbackRequest(call_id="c1", status="busy")
            )

            mock_dispatcher.release_call_slot.assert_not_awaited()
            mock_cb.record_and_evaluate.assert_not_awaited()
            mock_pub.publish_retry_needed.assert_not_awaited()
            # The final terminal-state write (is_completed/state/tags) also
            # should not re-run — only the audit-log append at the top of
            # the function (which always runs) uses update_workflow_run.
            for call in mock_db.update_workflow_run.await_args_list:
                assert "is_completed" not in call.kwargs


class TestCompletedBranchIdempotency:
    @pytest.mark.asyncio
    async def test_redelivered_completed_does_not_double_release_slot(self):
        workflow_run = _workflow_run(state="completed")
        with (
            patch("api.services.telephony.status_processor.db_client") as mock_db,
            patch(
                "api.services.telephony.status_processor.campaign_call_dispatcher"
            ) as mock_dispatcher,
            patch(
                "api.services.telephony.status_processor.circuit_breaker"
            ) as mock_cb,
        ):
            mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
            mock_db.update_workflow_run = AsyncMock()
            mock_dispatcher.release_call_slot = AsyncMock()
            mock_cb.record_and_evaluate = AsyncMock()

            await _process_status_update(
                1, StatusCallbackRequest(call_id="c1", status="completed")
            )

            mock_dispatcher.release_call_slot.assert_not_awaited()
            mock_cb.record_and_evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_completed_delivery_releases_slot_once(self):
        workflow_run = _workflow_run(state="initialized")
        with (
            patch("api.services.telephony.status_processor.db_client") as mock_db,
            patch(
                "api.services.telephony.status_processor.campaign_call_dispatcher"
            ) as mock_dispatcher,
            patch(
                "api.services.telephony.status_processor.circuit_breaker"
            ) as mock_cb,
        ):
            mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
            mock_db.update_workflow_run = AsyncMock()
            mock_dispatcher.release_call_slot = AsyncMock()
            mock_cb.record_and_evaluate = AsyncMock()

            await _process_status_update(
                1, StatusCallbackRequest(call_id="c1", status="completed")
            )

            mock_dispatcher.release_call_slot.assert_awaited_once_with(1)
            mock_cb.record_and_evaluate.assert_awaited_once_with(7, is_failure=False)
