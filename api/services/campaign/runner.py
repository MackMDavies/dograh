from datetime import UTC, datetime
from typing import Any, Dict

from loguru import logger

from api.db import db_client
from api.db.models import CampaignModel, UserModel
from api.services.campaign.campaign_event_publisher import (
    get_campaign_event_publisher,
)
from api.services.campaign.circuit_breaker import circuit_breaker
from api.services.quota_service import check_dograh_quota
from api.tasks.arq import enqueue_job
from api.tasks.function_names import FunctionNames


class CampaignValidationError(Exception):
    """Raised by validate_campaign_startable; carries an HTTP-shaped reason."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def validate_campaign_startable(campaign: CampaignModel, user: UserModel) -> None:
    """Everything that must be true before a campaign is allowed to start.

    Shared by the manual /start route and the scheduled-launch fire path in
    the orchestrator — the two must never validate differently, or a
    campaign that's fine to start by hand could silently start unchecked
    when its schedule fires (or vice versa).
    """
    if not campaign.telephony_configuration_id:
        configs = await db_client.list_telephony_configurations(campaign.organization_id)
        if not configs:
            raise CampaignValidationError(
                401,
                "You must configure telephony first by going to APP_URL/configure-telephony",
            )

    quota_result = await check_dograh_quota(user, workflow_id=campaign.workflow_id)
    if not quota_result.has_quota:
        raise CampaignValidationError(402, quota_result.error_message)


class CampaignRunnerService:
    """Orchestrates campaign execution"""

    async def start_campaign(self, campaign_id: int) -> None:
        """Entry point - updates state to 'syncing' and enqueues sync task"""
        # Get campaign
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        if campaign.state != "created":
            raise ValueError(
                f"Campaign must be in 'created' state to start, current state: {campaign.state}"
            )

        # Redial campaigns have queued_runs pre-seeded from the parent campaign,
        # so skip source sync and transition straight to 'running'.
        is_redial = bool(
            (campaign.orchestrator_metadata or {}).get("parent_campaign_id")
        )
        if is_redial:
            now = datetime.now(UTC)
            await db_client.update_campaign(
                campaign_id=campaign_id,
                state="running",
                started_at=now,
                source_last_synced_at=now,
            )
            publisher = await get_campaign_event_publisher()
            await publisher.publish_sync_completed(
                campaign_id=campaign_id,
                total_rows=campaign.total_rows or 0,
                source_type=campaign.source_type,
                source_id=campaign.source_id,
            )
            logger.info(f"Redial campaign {campaign_id} started, source sync skipped")
            return

        # Update campaign state to syncing
        await db_client.update_campaign(
            campaign_id=campaign_id,
            state="syncing",
            started_at=datetime.now(UTC),
            source_sync_status="in_progress",
        )

        # Enqueue the sync task
        await enqueue_job(FunctionNames.SYNC_CAMPAIGN_SOURCE, campaign_id)

        logger.info(f"Campaign {campaign_id} started, syncing source data")

    async def fire_scheduled_campaign(self, campaign_id: int) -> bool:
        """Called by the orchestrator when a scheduled campaign is due.

        Claims the campaign atomically (race-safe against a concurrent
        cancel/edit), then does the sync-enqueue work start_campaign does
        for the non-redial case (a scheduled campaign can never be a
        redial — those are created via a separate path that never sets
        scheduled_start_at). Returns False if the claim was lost to a
        race — the caller should treat that as "nothing to do", not an
        error.
        """
        claimed = await db_client.claim_scheduled_campaign(campaign_id)
        if not claimed:
            return False

        await enqueue_job(FunctionNames.SYNC_CAMPAIGN_SOURCE, campaign_id)
        logger.info(f"Scheduled campaign {campaign_id} fired, syncing source data")
        return True

    async def pause_campaign(self, campaign_id: int) -> None:
        """Pauses active campaign processing"""
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        if campaign.state not in ["running", "syncing"]:
            raise ValueError(
                f"Campaign must be in 'running' or 'syncing' state to pause, current state: {campaign.state}"
            )

        # Update state to paused
        await db_client.update_campaign(campaign_id=campaign_id, state="paused")

        logger.info(f"Campaign {campaign_id} paused")

    async def resume_campaign(self, campaign_id: int) -> None:
        """Resumes paused campaign"""
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        if campaign.state != "paused":
            raise ValueError(
                f"Campaign must be in 'paused' state to resume, current state: {campaign.state}"
            )

        # Update state to running. Do not queue batch since campaign orchestrator's
        # stale campaign checker would do that if there are pending work.
        await db_client.update_campaign(campaign_id=campaign_id, state="running")

        # Reset circuit breaker so the resumed campaign starts with a clean slate
        await circuit_breaker.reset(campaign_id)

        logger.info(f"Campaign {campaign_id} resumed")

    async def get_campaign_status(self, campaign_id: int) -> Dict[str, Any]:
        """Returns detailed campaign status"""
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        # Count failed calls from workflow runs
        failed_calls = await self._count_failed_campaign_calls(campaign_id)

        return {
            "campaign_id": campaign_id,
            "state": campaign.state,
            "total_rows": campaign.total_rows or 0,
            "processed_rows": campaign.processed_rows,
            "failed_calls": failed_calls,
            "progress_percentage": (
                (campaign.processed_rows / campaign.total_rows * 100)
                if campaign.total_rows and campaign.total_rows > 0
                else 0
            ),
            "source_sync": {
                "status": campaign.source_sync_status,
                "last_synced_at": campaign.source_last_synced_at,
                "error": campaign.source_sync_error,
            },
            "rate_limit": campaign.rate_limit_per_second,
            "started_at": campaign.started_at,
            "completed_at": campaign.completed_at,
        }

    async def _count_failed_campaign_calls(self, campaign_id: int) -> int:
        """Count failed calls by examining workflow_run telephony callbacks"""
        # Only the logs column is needed here, not the full row (cost_info,
        # initial_context, gathered_context, etc.) — this is polled
        # continuously by the frontend while a campaign is active.
        run_logs = await db_client.get_campaign_run_logs(campaign_id)

        failed_count = 0
        for logs in run_logs:
            callbacks = logs.get("telephony_status_callbacks", [])
            if callbacks:
                # Check final status
                final_status = callbacks[-1].get("status", "").lower()
                if final_status in ["failed", "busy", "no-answer"]:
                    failed_count += 1

        return failed_count


# Global instance
campaign_runner_service = CampaignRunnerService()
