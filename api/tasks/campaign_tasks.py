from datetime import UTC, datetime
from typing import Dict

from loguru import logger

from api.db import db_client
from api.services.campaign.campaign_call_dispatcher import campaign_call_dispatcher
from api.services.campaign.campaign_event_publisher import (
    get_campaign_event_publisher,
)
from api.services.campaign.errors import (
    ConcurrentSlotAcquisitionError,
    PhoneNumberPoolExhaustedError,
)
from api.services.campaign.source_sync_factory import get_sync_service

PHONE_NUMBER_POOL_EXHAUSTED_COUNTER_KEY = "phone_number_pool_exhausted_attempts"
MAX_PHONE_NUMBER_POOL_EXHAUSTED_ATTEMPTS = 3

CONCURRENT_SLOT_TIMEOUT_COUNTER_KEY = "concurrent_slot_acquisition_timeout_attempts"
MAX_CONCURRENT_SLOT_TIMEOUT_ATTEMPTS = 3


async def sync_campaign_source(ctx: Dict, campaign_id: int) -> None:
    """
    Phase 1: Syncs data from configured source to queued_runs table
    - Campaign state should already be 'syncing'
    - Determines source type from campaign configuration
    - Fetches data via the appropriate sync service
    - Creates queued_run entries with unique source_uuid
    - Updates campaign total_rows
    - Transitions campaign state to 'running' on success
    - Enqueues process_campaign_batch tasks
    """
    logger.info(f"Starting source sync for campaign {campaign_id}")

    try:
        # Get campaign
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        # Get appropriate sync service
        sync_service = get_sync_service(campaign.source_type)

        # Sync source data
        rows_synced = await sync_service.sync_source_data(campaign_id)

        if rows_synced == 0:
            # No data to process
            await db_client.update_campaign(
                campaign_id=campaign_id,
                state="completed",
                completed_at=datetime.now(UTC),
                source_sync_status="completed",
                source_last_synced_at=datetime.now(UTC),
            )
            logger.info(f"Campaign {campaign_id} completed with no data to process")
            return

        # Update campaign state to running
        await db_client.update_campaign(
            campaign_id=campaign_id,
            state="running",
            source_sync_status="completed",
            source_last_synced_at=datetime.now(UTC),
        )

        # Publish sync completed event - orchestrator will schedule first batch
        publisher = await get_campaign_event_publisher()
        await publisher.publish_sync_completed(
            campaign_id=campaign_id,
            total_rows=rows_synced,
            source_type=campaign.source_type,
            source_id=campaign.source_id,
        )

        logger.info(
            f"Campaign {campaign_id} source sync completed, {rows_synced} rows synced"
        )

    except Exception as e:
        logger.error(f"Error syncing campaign {campaign_id} source: {e}")

        # Update campaign with error
        await db_client.update_campaign(
            campaign_id=campaign_id,
            state="failed",
            source_sync_status="failed",
            source_sync_error=str(e),
        )
        await db_client.append_campaign_log(
            campaign_id=campaign_id,
            level="error",
            event="source_sync_failed",
            message=f"Source sync failed: {e}",
            details={"error": str(e)},
        )
        raise


async def process_campaign_batch(
    ctx: Dict, campaign_id: int, batch_size: int = 10
) -> None:
    """
    Phase 2: Processes a batch of queued runs
    - Fetches next batch of 'queued' runs (including due retries)
    - Creates workflow runs with context variables
    - Initiates Twilio calls with rate limiting
    - Updates queued_run state to 'processed'
    - Updates campaign.processed_rows counter
    - Publishes batch_completed event for orchestrator

    # TODO: May be not fail the campaign immediately on a single batch failure
    # and propagate the error to campaign orchestrator which can fail the campaign
    # on some consecutive batch failures.
    """
    logger.info(f"Processing batch for campaign {campaign_id}, batch_size={batch_size}")

    # failed_count used to be initialised to 0 here and never assigned again, so
    # BOTH the log line below and the batch_completed event always reported
    # failed=0 — even when every call in the batch failed. A batch of 10 dials
    # that all 403'd logged exactly like a batch that did nothing at all, which
    # is the worst possible failure mode for a system whose dial gates are
    # already hard to observe.
    #
    # process_batch() returns only the processed count and increments
    # campaign.failed_rows/suppressed_rows internally, so the honest per-batch
    # numbers are the deltas on those counters. Deliberately measured here
    # rather than by changing process_batch's signature: campaign_call_dispatcher
    # is concurrently being modified for calling-hours enforcement, and widening
    # its contract now would collide with that work.
    before = await db_client.get_campaign_by_id(campaign_id)
    failed_before = (before.failed_rows or 0) if before else 0
    suppressed_before = (before.suppressed_rows or 0) if before else 0

    failed_count = 0
    suppressed_count = 0
    try:
        # Process the batch
        processed_count = await campaign_call_dispatcher.process_batch(
            campaign_id=campaign_id, batch_size=batch_size
        )

        after = await db_client.get_campaign_by_id(campaign_id)
        if after:
            failed_count = max(0, (after.failed_rows or 0) - failed_before)
            suppressed_count = max(0, (after.suppressed_rows or 0) - suppressed_before)

        if processed_count > 0:
            await db_client.reset_campaign_metadata_counter(
                campaign_id=campaign_id,
                key=PHONE_NUMBER_POOL_EXHAUSTED_COUNTER_KEY,
            )
            await db_client.reset_campaign_metadata_counter(
                campaign_id=campaign_id,
                key=CONCURRENT_SLOT_TIMEOUT_COUNTER_KEY,
            )

        # Publish batch completed event - orchestrator will handle next batch scheduling
        publisher = await get_campaign_event_publisher()
        await publisher.publish_batch_completed(
            campaign_id=campaign_id,
            processed_count=processed_count,
            failed_count=failed_count,
            batch_size=batch_size,
        )

        logger.info(
            f"Campaign {campaign_id} batch completed: processed={processed_count}, "
            f"failed={failed_count}, suppressed={suppressed_count}"
        )

    except ConcurrentSlotAcquisitionError as e:
        # A concurrency squeeze is usually transient (another campaign or an
        # inbound call briefly using up the org's slots) — retry like
        # PhoneNumberPoolExhaustedError instead of failing the whole campaign
        # on the first timeout.
        attempt = await db_client.increment_campaign_metadata_counter(
            campaign_id=campaign_id,
            key=CONCURRENT_SLOT_TIMEOUT_COUNTER_KEY,
        )
        logger.warning(
            f"Failed to acquire concurrent slot for campaign {campaign_id}: {e}; "
            f"attempt={attempt}/{MAX_CONCURRENT_SLOT_TIMEOUT_ATTEMPTS}"
        )

        publisher = await get_campaign_event_publisher()

        if attempt < MAX_CONCURRENT_SLOT_TIMEOUT_ATTEMPTS:
            await db_client.append_campaign_log(
                campaign_id=campaign_id,
                level="warning",
                event="concurrent_slot_acquisition_timeout_retry",
                message=(
                    f"Concurrent slot acquisition timed out: {e}; retry attempt "
                    f"{attempt}/{MAX_CONCURRENT_SLOT_TIMEOUT_ATTEMPTS}"
                ),
                details={
                    "error": str(e),
                    "attempt": attempt,
                    "max_attempts": MAX_CONCURRENT_SLOT_TIMEOUT_ATTEMPTS,
                },
            )
            await publisher.publish_batch_completed(
                campaign_id=campaign_id,
                processed_count=0,
                failed_count=0,
                batch_size=batch_size,
            )
            return

        # Publish batch failed event with specific error
        await publisher.publish_batch_failed(
            campaign_id=campaign_id,
            error=f"Concurrent slot acquisition timeout: {e}",
            processed_count=0,
        )

        # Update campaign state to failed
        await db_client.update_campaign(campaign_id=campaign_id, state="failed")
        await db_client.append_campaign_log(
            campaign_id=campaign_id,
            level="error",
            event="batch_failed",
            message=(
                f"Concurrent slot acquisition timeout after {attempt} "
                f"consecutive attempts: {e}"
            ),
            details={
                "error": str(e),
                "reason": "concurrent_slot_timeout",
                "attempt": attempt,
                "max_attempts": MAX_CONCURRENT_SLOT_TIMEOUT_ATTEMPTS,
            },
        )
        raise

    except PhoneNumberPoolExhaustedError as e:
        attempt = await db_client.increment_campaign_metadata_counter(
            campaign_id=campaign_id,
            key=PHONE_NUMBER_POOL_EXHAUSTED_COUNTER_KEY,
        )
        logger.warning(
            f"Phone number pool exhausted for campaign {campaign_id}: {e}; "
            f"attempt={attempt}/{MAX_PHONE_NUMBER_POOL_EXHAUSTED_ATTEMPTS}"
        )

        publisher = await get_campaign_event_publisher()

        if attempt < MAX_PHONE_NUMBER_POOL_EXHAUSTED_ATTEMPTS:
            await db_client.append_campaign_log(
                campaign_id=campaign_id,
                level="warning",
                event="phone_number_pool_exhausted_retry",
                message=(
                    f"Phone number pool exhausted for org {e.organization_id}: "
                    "no free from_number available to dispatch outbound calls; "
                    f"retry attempt {attempt}/"
                    f"{MAX_PHONE_NUMBER_POOL_EXHAUSTED_ATTEMPTS}"
                ),
                details={
                    "error": str(e),
                    "organization_id": e.organization_id,
                    "attempt": attempt,
                    "max_attempts": MAX_PHONE_NUMBER_POOL_EXHAUSTED_ATTEMPTS,
                },
            )
            await publisher.publish_batch_completed(
                campaign_id=campaign_id,
                processed_count=0,
                failed_count=0,
                batch_size=batch_size,
            )
            return

        await publisher.publish_batch_failed(
            campaign_id=campaign_id,
            error=f"Phone number pool exhausted: {e}",
            processed_count=0,
        )

        await db_client.update_campaign(campaign_id=campaign_id, state="failed")
        await db_client.append_campaign_log(
            campaign_id=campaign_id,
            level="error",
            event="phone_number_pool_exhausted",
            message=(
                f"Phone number pool exhausted for org {e.organization_id} after "
                f"{attempt} consecutive attempts: no free from_number available "
                "to dispatch outbound calls"
            ),
            details={
                "error": str(e),
                "organization_id": e.organization_id,
                "attempt": attempt,
                "max_attempts": MAX_PHONE_NUMBER_POOL_EXHAUSTED_ATTEMPTS,
            },
        )
        raise

    except Exception as e:
        logger.error(f"Error processing batch for campaign {campaign_id}: {e}")

        # Publish batch failed event
        publisher = await get_campaign_event_publisher()
        await publisher.publish_batch_failed(
            campaign_id=campaign_id,
            error=str(e),
            processed_count=0,
        )

        # Update campaign state to failed
        await db_client.update_campaign(campaign_id=campaign_id, state="failed")
        await db_client.append_campaign_log(
            campaign_id=campaign_id,
            level="error",
            event="batch_failed",
            message=f"Batch processing failed: {e}",
            details={"error": str(e)},
        )
        raise
