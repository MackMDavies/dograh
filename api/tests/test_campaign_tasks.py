"""
Tests for api.tasks.campaign_tasks failure handling.

Specifically: each kind of failure that pauses or fails a campaign should
write a specific, identifiable entry into the campaign log so operators
can tell at a glance why a campaign stopped.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.campaign.errors import (
    ConcurrentSlotAcquisitionError,
    PhoneNumberPoolExhaustedError,
)
from api.tasks.campaign_tasks import process_campaign_batch


class TestProcessCampaignBatchFailureLogs:
    """``process_campaign_batch`` should log a *specific* event for each
    distinct failure mode, not collapse them all into a generic
    ``batch_failed`` entry."""

    @pytest.mark.asyncio
    async def test_phone_number_pool_exhausted_retries_before_final_failure(self):
        """The first two consecutive pool exhaustion attempts keep the
        campaign running and schedule another batch."""
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_disp.process_batch = AsyncMock(
                side_effect=PhoneNumberPoolExhaustedError(organization_id=7)
            )
            mock_db.increment_campaign_metadata_counter = AsyncMock(return_value=2)
            mock_db.get_campaign_by_id = AsyncMock(return_value=None)
            mock_db.update_campaign = AsyncMock()
            mock_db.append_campaign_log = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await process_campaign_batch({}, campaign_id=42)

            mock_db.update_campaign.assert_not_awaited()
            mock_pub.publish_batch_failed.assert_not_awaited()
            mock_pub.publish_batch_completed.assert_awaited_once_with(
                campaign_id=42,
                processed_count=0,
                failed_count=0,
                batch_size=10,
            )

            mock_db.append_campaign_log.assert_called_once()
            kwargs = mock_db.append_campaign_log.call_args.kwargs
            assert kwargs["campaign_id"] == 42
            assert kwargs["event"] == "phone_number_pool_exhausted_retry"
            assert kwargs["level"] == "warning"
            assert kwargs["details"]["organization_id"] == 7
            assert kwargs["details"]["attempt"] == 2

    @pytest.mark.asyncio
    async def test_phone_number_pool_exhausted_fails_on_third_attempt(self):
        """The third consecutive pool exhaustion attempt marks the campaign
        failed with a specific operator-facing log entry."""
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_disp.process_batch = AsyncMock(
                side_effect=PhoneNumberPoolExhaustedError(organization_id=7)
            )
            mock_db.increment_campaign_metadata_counter = AsyncMock(return_value=3)
            mock_db.get_campaign_by_id = AsyncMock(return_value=None)
            mock_db.update_campaign = AsyncMock()
            mock_db.append_campaign_log = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            with pytest.raises(PhoneNumberPoolExhaustedError):
                await process_campaign_batch({}, campaign_id=42)

            mock_db.update_campaign.assert_called_once_with(
                campaign_id=42, state="failed"
            )
            mock_pub.publish_batch_failed.assert_awaited_once()

            mock_db.append_campaign_log.assert_called_once()
            kwargs = mock_db.append_campaign_log.call_args.kwargs
            assert kwargs["campaign_id"] == 42
            assert kwargs["event"] == "phone_number_pool_exhausted"
            assert kwargs["level"] == "error"
            assert "phone number" in kwargs["message"].lower()
            assert kwargs["details"]["organization_id"] == 7
            assert kwargs["details"]["attempt"] == 3

    @pytest.mark.asyncio
    async def test_concurrent_slot_timeout_retries_before_final_failure(self):
        """A concurrency squeeze is usually transient — the first two
        consecutive timeouts keep the campaign running and schedule another
        batch, same as phone-number-pool exhaustion."""
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_disp.process_batch = AsyncMock(
                side_effect=ConcurrentSlotAcquisitionError(
                    organization_id=7, campaign_id=42, wait_time=30.0
                )
            )
            mock_db.increment_campaign_metadata_counter = AsyncMock(return_value=2)
            mock_db.get_campaign_by_id = AsyncMock(return_value=None)
            mock_db.update_campaign = AsyncMock()
            mock_db.append_campaign_log = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await process_campaign_batch({}, campaign_id=42)

            mock_db.update_campaign.assert_not_awaited()
            mock_pub.publish_batch_failed.assert_not_awaited()
            mock_pub.publish_batch_completed.assert_awaited_once_with(
                campaign_id=42,
                processed_count=0,
                failed_count=0,
                batch_size=10,
            )

            mock_db.append_campaign_log.assert_called_once()
            kwargs = mock_db.append_campaign_log.call_args.kwargs
            assert kwargs["campaign_id"] == 42
            assert kwargs["event"] == "concurrent_slot_acquisition_timeout_retry"
            assert kwargs["level"] == "warning"
            assert kwargs["details"]["attempt"] == 2

    @pytest.mark.asyncio
    async def test_concurrent_slot_timeout_fails_on_third_attempt(self):
        """The third consecutive concurrency-slot timeout marks the campaign
        failed with a specific operator-facing log entry."""
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_disp.process_batch = AsyncMock(
                side_effect=ConcurrentSlotAcquisitionError(
                    organization_id=7, campaign_id=42, wait_time=30.0
                )
            )
            mock_db.increment_campaign_metadata_counter = AsyncMock(return_value=3)
            mock_db.get_campaign_by_id = AsyncMock(return_value=None)
            mock_db.update_campaign = AsyncMock()
            mock_db.append_campaign_log = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            with pytest.raises(ConcurrentSlotAcquisitionError):
                await process_campaign_batch({}, campaign_id=42)

            mock_db.update_campaign.assert_called_once_with(
                campaign_id=42, state="failed"
            )
            mock_pub.publish_batch_failed.assert_awaited_once()

            mock_db.append_campaign_log.assert_called_once()
            kwargs = mock_db.append_campaign_log.call_args.kwargs
            assert kwargs["event"] == "batch_failed"
            assert kwargs["details"]["reason"] == "concurrent_slot_timeout"
            assert kwargs["details"]["attempt"] == 3


class TestBatchCompletedCounts:
    """``process_campaign_batch`` must report the batch's REAL failure count.

    Regression: ``failed_count`` was initialised to 0 and never assigned, so a
    batch in which every dial failed published and logged ``failed=0`` —
    indistinguishable from a batch that did nothing. Found on production when a
    campaign whose only call died on a Twilio 403 logged
    ``processed=0, failed=0`` while campaign.failed_rows was 1.
    """

    @staticmethod
    def _campaign(failed_rows: int, suppressed_rows: int = 0):
        c = SimpleNamespace()
        c.failed_rows = failed_rows
        c.suppressed_rows = suppressed_rows
        return c

    @pytest.mark.asyncio
    async def test_failed_count_reflects_failed_rows_delta(self):
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            # Nothing processed; the one row in the batch failed.
            mock_disp.process_batch = AsyncMock(return_value=0)
            mock_db.get_campaign_by_id = AsyncMock(
                side_effect=[self._campaign(0), self._campaign(1)]
            )
            mock_db.reset_campaign_metadata_counter = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await process_campaign_batch({}, campaign_id=42)

            kwargs = mock_pub.publish_batch_completed.call_args.kwargs
            assert kwargs["processed_count"] == 0
            assert kwargs["failed_count"] == 1, (
                "a batch whose only dial failed must not report failed=0"
            )

    @pytest.mark.asyncio
    async def test_counts_are_per_batch_not_cumulative(self):
        """The delta matters, not the campaign's lifetime total — a campaign
        that already had 5 failures and adds 2 reports 2, not 7."""
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_disp.process_batch = AsyncMock(return_value=3)
            mock_db.get_campaign_by_id = AsyncMock(
                side_effect=[self._campaign(5, 1), self._campaign(7, 4)]
            )
            mock_db.reset_campaign_metadata_counter = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await process_campaign_batch({}, campaign_id=42)

            kwargs = mock_pub.publish_batch_completed.call_args.kwargs
            assert kwargs["failed_count"] == 2
            assert kwargs["processed_count"] == 3

    @pytest.mark.asyncio
    async def test_missing_campaign_row_does_not_crash_the_batch(self):
        """get_campaign_by_id returning None must not take down a batch that
        otherwise succeeded — the counters are observability, not control flow."""
        with (
            patch("api.tasks.campaign_tasks.campaign_call_dispatcher") as mock_disp,
            patch("api.tasks.campaign_tasks.db_client") as mock_db,
            patch(
                "api.tasks.campaign_tasks.get_campaign_event_publisher"
            ) as mock_get_pub,
        ):
            mock_disp.process_batch = AsyncMock(return_value=2)
            mock_db.get_campaign_by_id = AsyncMock(side_effect=[None, None])
            mock_db.reset_campaign_metadata_counter = AsyncMock()
            mock_pub = AsyncMock()
            mock_get_pub.return_value = mock_pub

            await process_campaign_batch({}, campaign_id=42)

            kwargs = mock_pub.publish_batch_completed.call_args.kwargs
            assert kwargs["failed_count"] == 0
            assert kwargs["processed_count"] == 2
