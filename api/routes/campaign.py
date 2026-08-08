import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from api.constants import (
    DEFAULT_CAMPAIGN_RETRY_CONFIG,
    MAX_SYSTEM_CONCURRENCY,
)
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.campaign.caller_id_capacity import (
    effective_concurrency_limit,
    get_calls_per_number,
)
from api.services.campaign.runner import (
    CampaignValidationError,
    campaign_runner_service,
    validate_campaign_startable,
)
from api.services.campaign.source_sync import CampaignSourceSyncService
from api.services.org_concurrency import get_org_concurrency_limit
from api.services.campaign.source_sync_factory import get_sync_service
from api.services.quota_service import check_dograh_quota
from api.services.reports import generate_campaign_report_csv
from api.services.storage import storage_fs

router = APIRouter(prefix="/campaign")


async def _get_from_numbers_count(organization_id: int) -> int:
    """Active phone-number count from the org's default telephony config.
    Used to validate ``max_concurrency`` against caller-id supply."""
    try:
        default_cfg = await db_client.get_default_telephony_configuration(
            organization_id
        )
        if default_cfg:
            addresses = await db_client.list_active_normalized_addresses_for_config(
                default_cfg.id
            )
            return len(addresses)
    except Exception:
        pass
    return 0


async def _validate_max_concurrency(max_concurrency: int, organization_id: int) -> None:
    """Validate max_concurrency against the org limit and caller-ID capacity.

    Caller-ID supply only bounds concurrency when the org has opted into a
    per-CLI cap (``CALLS_PER_NUMBER``); by default one number can carry the
    org's whole limit, so a single configured DID no longer pins a campaign to
    a concurrency of 1.

    Raises HTTPException(400) if the value exceeds the effective limit.
    """
    org_limit = await get_org_concurrency_limit(organization_id)
    calls_per_number = await get_calls_per_number(organization_id)
    from_numbers_count = await _get_from_numbers_count(organization_id)
    effective_limit = effective_concurrency_limit(
        org_limit, from_numbers_count, calls_per_number
    )
    if max_concurrency > effective_limit:
        if effective_limit < org_limit:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"max_concurrency ({max_concurrency}) cannot exceed "
                    f"{effective_limit}. You have {from_numbers_count} phone "
                    f"number(s) configured and this organization allows "
                    f"{calls_per_number} simultaneous call(s) per number. Add "
                    "more CLIs in telephony configuration, or raise the "
                    "calls-per-number setting, to increase concurrency."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail=f"max_concurrency ({max_concurrency}) cannot exceed organization limit ({effective_limit})",
        )


def _validate_schedule_lead_time(scheduled_start_at: datetime) -> None:
    """Raise HTTPException(400) unless scheduled_start_at is tz-aware and at
    least 2 minutes in the future. Shared by campaign creation and schedule
    editing so both enforce the identical minimum lead time."""
    if scheduled_start_at.tzinfo is None:
        raise HTTPException(status_code=400, detail="scheduled_start_at must include a UTC offset")
    min_lead = datetime.now(timezone.utc) + timedelta(minutes=2)
    if scheduled_start_at < min_lead:
        raise HTTPException(
            status_code=400, detail="scheduled_start_at must be at least 2 minutes in the future"
        )


class RetryConfigRequest(BaseModel):
    enabled: bool = True
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_delay_seconds: int = Field(default=120, ge=30, le=3600)
    retry_on_busy: bool = True
    retry_on_no_answer: bool = True
    retry_on_voicemail: bool = True


class RetryConfigResponse(BaseModel):
    enabled: bool
    max_retries: int
    retry_delay_seconds: int
    retry_on_busy: bool
    retry_on_no_answer: bool
    retry_on_voicemail: bool


class TimeSlotRequest(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")

    @model_validator(mode="after")
    def validate_times(self):
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class ScheduleConfigRequest(BaseModel):
    enabled: bool = True
    timezone: str = "UTC"
    slots: List[TimeSlotRequest] = Field(..., min_length=1, max_length=50)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (KeyError, Exception):
            raise ValueError(f"Invalid timezone: {v}")
        return v


class TimeSlotResponse(BaseModel):
    day_of_week: int
    start_time: str
    end_time: str


class ScheduleConfigResponse(BaseModel):
    enabled: bool
    timezone: str
    slots: List[TimeSlotResponse]


class CallingHoursConfigRequest(BaseModel):
    """A campaign's override of the account's default calling-hours
    compliance window (see checkDialPermission / dograh-pre-call-check on
    the Sysevo side, and check_dial_permitted/campaign_call_dispatcher.py
    here). NOT the same thing as ScheduleConfigRequest above — that gates
    whether the campaign dials AT ALL right now (one fixed timezone,
    day-of-week slots); this gates each individual contact's dial against
    their own local time, intersected with a legal-floor minimum.

    mode="inherit"  -> use the account's own default (client_accounts.
                        outbound_calling_hours_*)
    mode="custom"   -> start/end below, still intersected with the legal floor
    mode="off"      -> no restriction at all, INCLUDING the legal floor —
                        off_acknowledged_at must be set (the caller has
                        already surfaced the compliance warning and required
                        an explicit checkbox before sending this); this is
                        the audit-trail record the design spec requires,
                        surfaced read-only on the Compliance page.
    """

    mode: Literal["inherit", "custom", "off"]
    start: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    off_acknowledged_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_custom_has_both_times(self) -> "CallingHoursConfigRequest":
        if self.mode == "custom":
            if not self.start or not self.end:
                raise ValueError("mode='custom' requires both start and end")
            # Overnight windows (e.g. 22:00-06:00) aren't supported — same
            # limitation as the sibling TimeSlotRequest.validate_times above.
            if self.start >= self.end:
                raise ValueError("start must be before end")
        return self

    @model_validator(mode="after")
    def validate_off_has_acknowledgment(self) -> "CallingHoursConfigRequest":
        if self.mode == "off" and self.off_acknowledged_at is None:
            raise ValueError("mode='off' requires off_acknowledged_at")
        return self


class CallingHoursConfigResponse(BaseModel):
    # str, not Literal — this reads back JSONB metadata written before/outside
    # model validation (mirrors ScheduleConfigResponse.timezone: str above).
    mode: str
    start: Optional[str] = None
    end: Optional[str] = None
    off_acknowledged_at: Optional[datetime] = None


class CircuitBreakerConfigRequest(BaseModel):
    enabled: bool = True
    failure_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    window_seconds: int = Field(default=120, ge=30, le=600)
    min_calls_in_window: int = Field(default=5, ge=1, le=100)


class CircuitBreakerConfigResponse(BaseModel):
    enabled: bool = False
    failure_threshold: float = 0.5
    window_seconds: int = 120
    min_calls_in_window: int = 5


class EnqueueRunRequest(BaseModel):
    source_uuid: str
    context_variables: dict
    scheduled_for: Optional[datetime] = None
    retry_reason: Optional[str] = None


class UpdateQueuedRunRequest(BaseModel):
    """SYSEVO_CALLBACK_PATCH: reschedule, or cancel, a queued callback.

    `reschedule` is explicit rather than inferred from `scheduled_for is not
    None`, because None is a meaningful value here: clearing the time sends the
    row back to the review queue, and the dispatcher only claims rows whose time
    is due, so an unscheduled row can never dial.
    """

    scheduled_for: Optional[datetime] = None
    reschedule: bool = False
    cancel: bool = False


class CreateCampaignRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    workflow_id: int
    source_type: str = Field(..., pattern="^csv$")
    source_id: str  # CSV file key
    # Optional during the legacy → multi-config migration window. Required in
    # a follow-up. When omitted, the dispatcher falls back to the org's
    # default config.
    telephony_configuration_id: Optional[int] = None
    retry_config: Optional[RetryConfigRequest] = None
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=MAX_SYSTEM_CONCURRENCY)
    schedule_config: Optional[ScheduleConfigRequest] = None
    circuit_breaker: Optional[CircuitBreakerConfigRequest] = None
    calling_hours: Optional[CallingHoursConfigRequest] = None
    # A future launch instant. When set, the campaign is created in
    # 'scheduled' state and NOT auto-started — the orchestrator fires it.
    scheduled_start_at: Optional[datetime] = None
    scheduled_timezone: Optional[str] = None

    @field_validator("scheduled_timezone")
    @classmethod
    def validate_scheduled_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (KeyError, Exception):
            raise ValueError(f"Invalid timezone: {v}")
        return v

    @model_validator(mode="after")
    def validate_schedule_fields_together(self) -> "CreateCampaignRequest":
        if (self.scheduled_start_at is None) != (self.scheduled_timezone is None):
            raise ValueError("scheduled_start_at and scheduled_timezone must be set together")
        return self


class EnqueueRunRequest(BaseModel):
    source_uuid: str
    context_variables: dict
    scheduled_for: Optional[datetime] = None
    retry_reason: Optional[str] = None


class UpdateCampaignRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    retry_config: Optional[RetryConfigRequest] = None
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=MAX_SYSTEM_CONCURRENCY)
    schedule_config: Optional[ScheduleConfigRequest] = None
    circuit_breaker: Optional[CircuitBreakerConfigRequest] = None
    calling_hours: Optional[CallingHoursConfigRequest] = None
    workflow_id: Optional[int] = None
    telephony_configuration_id: Optional[int] = None


class UpdateCampaignScheduleRequest(BaseModel):
    scheduled_start_at: datetime
    scheduled_timezone: str = Field(..., min_length=1)

    @field_validator("scheduled_timezone")
    @classmethod
    def validate_scheduled_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (KeyError, Exception):
            raise ValueError(f"Invalid timezone: {v}")
        return v


class CampaignLogEntryResponse(BaseModel):
    """A single timestamped entry from the campaign's append-only log.

    Surfaced in the UI so operators can see why a campaign moved to
    paused / failed without digging through server logs.
    """

    ts: str
    level: str
    event: str
    message: str
    details: Optional[Dict[str, Any]] = None


class CampaignResponse(BaseModel):
    id: int
    name: str
    workflow_id: int
    workflow_name: str
    state: str
    source_type: str
    source_id: str
    total_rows: Optional[int]
    processed_rows: int
    failed_rows: int
    suppressed_rows: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    scheduled_start_at: Optional[datetime] = None
    scheduled_timezone: Optional[str] = None
    retry_config: RetryConfigResponse
    max_concurrency: Optional[int] = None
    schedule_config: Optional[ScheduleConfigResponse] = None
    circuit_breaker: Optional[CircuitBreakerConfigResponse] = None
    calling_hours: Optional[CallingHoursConfigResponse] = None
    executed_count: int = 0
    total_queued_count: int = 0
    parent_campaign_id: Optional[int] = None
    redialed_campaign_id: Optional[int] = None
    # SYSEVO_IS_STANDING: a standing campaign is a dispatch queue (callbacks),
    # not a user-facing campaign. Exposed so the UI can route it to /callbacks
    # and keep it out of the campaign list.
    is_standing: bool = False
    telephony_configuration_id: Optional[int] = None
    telephony_configuration_name: Optional[str] = None
    logs: List[CampaignLogEntryResponse] = Field(default_factory=list)


class CampaignsResponse(BaseModel):
    campaigns: List[CampaignResponse]


class WorkflowRunResponse(BaseModel):
    id: int
    workflow_id: int
    state: str
    created_at: datetime
    completed_at: Optional[datetime]


class CampaignRunsResponse(BaseModel):
    """Paginated response for campaign workflow runs"""

    runs: List[dict]  # WorkflowRunResponseSchema from schemas
    total_count: int
    page: int
    limit: int
    total_pages: int


class CampaignProgressResponse(BaseModel):
    campaign_id: int
    state: str
    total_rows: int
    processed_rows: int
    failed_calls: int
    progress_percentage: float
    source_sync: dict
    rate_limit: int
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


# Default retry config for campaigns


def _build_campaign_response(
    campaign,
    workflow_name: str,
    executed_count: int = 0,
    total_queued_count: int = 0,
    telephony_configuration_name: Optional[str] = None,
) -> CampaignResponse:
    """Build a CampaignResponse from a campaign model."""
    # Get retry_config from campaign or use defaults
    retry_config = (
        campaign.retry_config
        if campaign.retry_config
        else DEFAULT_CAMPAIGN_RETRY_CONFIG
    )

    # Get max_concurrency, schedule_config, circuit_breaker from orchestrator_metadata
    max_concurrency = None
    schedule_config = None
    circuit_breaker_config = CircuitBreakerConfigResponse()
    calling_hours_config = None
    parent_campaign_id = None
    redialed_campaign_id = None
    if campaign.orchestrator_metadata:
        max_concurrency = campaign.orchestrator_metadata.get("max_concurrency")
        sc = campaign.orchestrator_metadata.get("schedule_config")
        if sc:
            schedule_config = ScheduleConfigResponse(
                enabled=sc.get("enabled", False),
                timezone=sc.get("timezone", "UTC"),
                slots=[TimeSlotResponse(**slot) for slot in sc.get("slots", [])],
            )
        cb = campaign.orchestrator_metadata.get("circuit_breaker")
        if cb:
            circuit_breaker_config = CircuitBreakerConfigResponse(**cb)
        ch_mode = campaign.orchestrator_metadata.get("calling_hours_mode")
        if ch_mode:
            calling_hours_config = CallingHoursConfigResponse(
                mode=ch_mode,
                start=campaign.orchestrator_metadata.get("calling_hours_start"),
                end=campaign.orchestrator_metadata.get("calling_hours_end"),
                off_acknowledged_at=campaign.orchestrator_metadata.get("calling_hours_off_acknowledged_at"),
            )
        parent_campaign_id = campaign.orchestrator_metadata.get("parent_campaign_id")
        redialed_campaign_id = campaign.orchestrator_metadata.get(
            "redialed_campaign_id"
        )

    return CampaignResponse(
        id=campaign.id,
        name=campaign.name,
        workflow_id=campaign.workflow_id,
        workflow_name=workflow_name,
        state=campaign.state,
        source_type=campaign.source_type,
        source_id=campaign.source_id,
        total_rows=campaign.total_rows,
        processed_rows=campaign.processed_rows,
        failed_rows=campaign.failed_rows,
        suppressed_rows=campaign.suppressed_rows,
        created_at=campaign.created_at,
        started_at=campaign.started_at,
        completed_at=campaign.completed_at,
        scheduled_start_at=campaign.scheduled_start_at,
        scheduled_timezone=campaign.scheduled_timezone,
        retry_config=RetryConfigResponse(**retry_config),
        max_concurrency=max_concurrency,
        schedule_config=schedule_config,
        circuit_breaker=circuit_breaker_config,
        calling_hours=calling_hours_config,
        executed_count=executed_count,
        total_queued_count=total_queued_count,
        parent_campaign_id=parent_campaign_id,
        redialed_campaign_id=redialed_campaign_id,
        is_standing=bool(getattr(campaign, "is_standing", False)),
        telephony_configuration_id=campaign.telephony_configuration_id,
        telephony_configuration_name=telephony_configuration_name,
        logs=[
            CampaignLogEntryResponse(**entry)
            for entry in (campaign.logs or [])
            if isinstance(entry, dict)
        ],
    )


async def _get_campaign_stats(campaign_id: int) -> tuple[int, int]:
    """Return (executed_count, total_queued_count) for a campaign."""
    stats_map = await db_client.get_queued_runs_stats_for_campaigns([campaign_id])
    s = stats_map.get(campaign_id, {})
    return s.get("executed", 0), s.get("total", 0)


async def _get_telephony_configuration_name(
    config_id: Optional[int], organization_id: Optional[int]
) -> Optional[str]:
    """Resolve the display name for a campaign's telephony configuration."""
    if config_id is None:
        return None
    if organization_id is None:
        # Superuser — unscoped lookup
        cfg = await db_client.get_telephony_configuration(config_id)
    else:
        cfg = await db_client.get_telephony_configuration_for_org(
            config_id, organization_id
        )
    return cfg.name if cfg else None


@router.post("/create")
async def create_campaign(
    request: CreateCampaignRequest,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Create a new campaign"""
    # Verify workflow exists — superusers can reference any org's workflow
    if user.is_superuser:
        workflow_name = await db_client.get_workflow_name(request.workflow_id)
    else:
        # Scope by ORGANISATION, not by who created the agent.
        #
        # This passed user.id positionally, which lands on get_workflow_name's
        # user_id parameter and filters WorkflowModel.user_id == user.id. So a
        # campaign could only be built from an agent you personally created:
        # anyone else in the org got 404 "Workflow not found" for an agent
        # sitting right there in their list. That hit sales managers and client
        # team members, the same way run creation did.
        #
        # update_campaign below already scopes this by organisation; this call
        # was the odd one out, even though the comment there cites it as the
        # example to follow.
        workflow_name = await db_client.get_workflow_name(
            request.workflow_id, organization_id=user.selected_organization_id
        )
    if not workflow_name:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Validate source data (phone_number column and format)
    sync_service = get_sync_service(request.source_type)
    validation_result = await sync_service.validate_source(
        request.source_id, user.selected_organization_id
    )
    if not validation_result.is_valid:
        raise HTTPException(status_code=400, detail=validation_result.error.message)

    # Validate template variables against source data columns
    workflow = await (
        db_client.get_workflow_by_id(request.workflow_id)
        if user.is_superuser
        else db_client.get_workflow(request.workflow_id, organization_id=user.selected_organization_id)
    )
    if workflow:
        from api.services.workflow.dto import ReactFlowDTO
        from api.services.workflow.workflow_graph import WorkflowGraph

        workflow_def = workflow.released_definition.workflow_json
        if workflow_def:
            try:
                dto = ReactFlowDTO(**workflow_def)
                graph = WorkflowGraph(dto)
                required_vars = graph.get_required_template_variables()

                if (
                    required_vars
                    and validation_result.headers
                    and validation_result.rows
                ):
                    template_validation = (
                        CampaignSourceSyncService.validate_template_columns(
                            validation_result.headers,
                            validation_result.rows,
                            required_vars,
                        )
                    )
                    if not template_validation.is_valid:
                        raise HTTPException(
                            status_code=400,
                            detail=template_validation.error.message,
                        )
            except HTTPException:
                raise
            except Exception:
                pass  # Don't block campaign creation if template extraction fails

    if request.max_concurrency is not None:
        await _validate_max_concurrency(
            request.max_concurrency, user.selected_organization_id
        )

    # Resolve which telephony config the campaign is pinned to. Explicit value
    # wins; otherwise default to the org's default config so legacy clients keep
    # working through the migration window.
    telephony_configuration_id = request.telephony_configuration_id
    if telephony_configuration_id:
        if user.is_superuser:
            cfg = await db_client.get_telephony_configuration(telephony_configuration_id)
        else:
            cfg = await db_client.get_telephony_configuration_for_org(
                telephony_configuration_id, user.selected_organization_id
            )
        if not cfg:
            raise HTTPException(
                status_code=400, detail="telephony_configuration_not_found"
            )
    else:
        default_cfg = await db_client.get_default_telephony_configuration(
            user.selected_organization_id
        )
        if default_cfg:
            telephony_configuration_id = default_cfg.id

    # Build retry_config dict if provided
    retry_config = None
    if request.retry_config:
        retry_config = request.retry_config.model_dump()

    # Build schedule_config dict if provided
    schedule_config = None
    if request.schedule_config:
        schedule_config = request.schedule_config.model_dump()

    # Build circuit_breaker dict if provided
    circuit_breaker_config = None
    if request.circuit_breaker:
        circuit_breaker_config = request.circuit_breaker.model_dump()

    # Build calling_hours dict if provided
    calling_hours_config = None
    if request.calling_hours:
        calling_hours_config = request.calling_hours.model_dump()

    if request.scheduled_start_at is not None:
        _validate_schedule_lead_time(request.scheduled_start_at)

    campaign = await db_client.create_campaign(
        name=request.name,
        workflow_id=request.workflow_id,
        source_type=request.source_type,
        source_id=request.source_id,
        user_id=user.id,
        organization_id=user.selected_organization_id,
        retry_config=retry_config,
        max_concurrency=request.max_concurrency,
        schedule_config=schedule_config,
        circuit_breaker=circuit_breaker_config,
        calling_hours_config=calling_hours_config,
        telephony_configuration_id=telephony_configuration_id,
        scheduled_start_at=request.scheduled_start_at,
        scheduled_timezone=request.scheduled_timezone,
    )

    cfg_name = await _get_telephony_configuration_name(
        campaign.telephony_configuration_id, user.selected_organization_id
    )
    return _build_campaign_response(
        campaign, workflow_name, telephony_configuration_name=cfg_name
    )


@router.get("/")
async def get_campaigns(
    user: UserModel = Depends(get_user),
) -> CampaignsResponse:
    """Get campaigns for user's organization"""
    campaigns = await db_client.get_campaigns(user.selected_organization_id)

    # Get workflow names for all campaigns
    workflow_ids = list(set(c.workflow_id for c in campaigns))
    workflows = await db_client.get_workflows_by_ids(
        workflow_ids, user.selected_organization_id
    )
    workflow_map = {w.id: w.name for w in workflows}

    stats_map = await db_client.get_queued_runs_stats_for_campaigns(
        [c.id for c in campaigns]
    )

    # Build {config_id: name} map by fetching all configs for the org once,
    # rather than one lookup per campaign.
    org_configs = await db_client.list_telephony_configurations(
        user.selected_organization_id
    )
    config_name_map = {cfg.id: cfg.name for cfg in org_configs}

    campaign_responses = [
        _build_campaign_response(
            c,
            workflow_map.get(c.workflow_id, "Unknown"),
            executed_count=stats_map.get(c.id, {}).get("executed", 0),
            total_queued_count=stats_map.get(c.id, {}).get("total", 0),
            telephony_configuration_name=config_name_map.get(
                c.telephony_configuration_id
            ),
        )
        for c in campaigns
    ]

    return CampaignsResponse(campaigns=campaign_responses)


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Get campaign details"""
    if user.is_superuser:
        campaign = await db_client.get_campaign_by_id(campaign_id)
    else:
        campaign = await db_client.get_campaign(campaign_id, user.selected_organization_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    workflow_name = await db_client.get_workflow_name(
        campaign.workflow_id, organization_id=campaign.organization_id
    )

    executed, total = await _get_campaign_stats(campaign.id)
    cfg_name = await _get_telephony_configuration_name(
        campaign.telephony_configuration_id, campaign.organization_id
    )
    return _build_campaign_response(
        campaign,
        workflow_name or "Unknown",
        executed,
        total,
        telephony_configuration_name=cfg_name,
    )


@router.post("/{campaign_id}/start")
async def start_campaign(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Start campaign execution"""
    org_id = None if user.is_superuser else user.selected_organization_id

    # Load campaign first so we can check telephony against the campaign's org.
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        await validate_campaign_startable(campaign, user)
    except CampaignValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    # Start the campaign using the runner service
    try:
        await campaign_runner_service.start_campaign(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get updated campaign
    campaign = await db_client.get_campaign(campaign_id, org_id)
    workflow_name = await db_client.get_workflow_name(
        campaign.workflow_id, organization_id=campaign.organization_id
    )

    executed, total = await _get_campaign_stats(campaign.id)
    cfg_name = await _get_telephony_configuration_name(
        campaign.telephony_configuration_id, campaign.organization_id
    )
    return _build_campaign_response(
        campaign,
        workflow_name or "Unknown",
        executed,
        total,
        telephony_configuration_name=cfg_name,
    )


@router.patch("/{campaign_id}/schedule")
async def update_campaign_schedule(
    campaign_id: int,
    request: UpdateCampaignScheduleRequest,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Edit a still-pending scheduled launch time."""
    org_id = None if user.is_superuser else user.selected_organization_id
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.state != "scheduled":
        raise HTTPException(
            status_code=400,
            detail=f"Campaign must be 'scheduled' to edit its launch time, current state: {campaign.state}",
        )

    _validate_schedule_lead_time(request.scheduled_start_at)

    updated = await db_client.update_campaign_schedule(
        campaign_id, request.scheduled_start_at, request.scheduled_timezone
    )
    if not updated:
        # Lost a race with the orchestrator firing it in the same instant.
        raise HTTPException(status_code=409, detail="Campaign is no longer scheduled")

    workflow_name = await db_client.get_workflow_name(
        updated.workflow_id, organization_id=updated.organization_id
    )
    executed, total = await _get_campaign_stats(updated.id)
    cfg_name = await _get_telephony_configuration_name(
        updated.telephony_configuration_id, updated.organization_id
    )
    return _build_campaign_response(
        updated, workflow_name or "Unknown", executed, total, telephony_configuration_name=cfg_name
    )


@router.post("/{campaign_id}/cancel-schedule")
async def cancel_campaign_schedule(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Cancel a pending schedule, reverting to 'created' (manual Start available)."""
    org_id = None if user.is_superuser else user.selected_organization_id
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.state != "scheduled":
        raise HTTPException(
            status_code=400,
            detail=f"Campaign must be 'scheduled' to cancel its launch, current state: {campaign.state}",
        )

    updated = await db_client.cancel_campaign_schedule(campaign_id)
    if not updated:
        raise HTTPException(status_code=409, detail="Campaign is no longer scheduled")

    workflow_name = await db_client.get_workflow_name(
        updated.workflow_id, organization_id=updated.organization_id
    )
    executed, total = await _get_campaign_stats(updated.id)
    cfg_name = await _get_telephony_configuration_name(
        updated.telephony_configuration_id, updated.organization_id
    )
    return _build_campaign_response(
        updated, workflow_name or "Unknown", executed, total, telephony_configuration_name=cfg_name
    )


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Pause campaign execution"""
    org_id = None if user.is_superuser else user.selected_organization_id
    # Verify campaign exists and belongs to organization
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Pause the campaign using the runner service
    try:
        await campaign_runner_service.pause_campaign(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get updated campaign
    campaign = await db_client.get_campaign(campaign_id, org_id)
    workflow_name = await db_client.get_workflow_name(
        campaign.workflow_id, organization_id=campaign.organization_id
    )

    executed, total = await _get_campaign_stats(campaign.id)
    cfg_name = await _get_telephony_configuration_name(
        campaign.telephony_configuration_id, campaign.organization_id
    )
    return _build_campaign_response(
        campaign,
        workflow_name or "Unknown",
        executed,
        total,
        telephony_configuration_name=cfg_name,
    )


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    request: UpdateCampaignRequest,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Update campaign settings (name, retry config, max concurrency, schedule)"""
    org_id = None if user.is_superuser else user.selected_organization_id
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Running/paused campaigns cannot have concurrency or schedule changed mid-flight.
    # Failed campaigns must remain fully editable so the agent/telephony/concurrency
    # can be corrected before relaunching. Completed campaigns are terminal — block
    # config changes there only.
    non_name_fields_requested = (
        request.retry_config is not None
        or request.max_concurrency is not None
        or request.schedule_config is not None
        or request.circuit_breaker is not None
        or request.calling_hours is not None
    )
    if non_name_fields_requested and campaign.state == "completed":
        raise HTTPException(
            status_code=400,
            detail="Cannot update settings on a completed campaign",
        )

    if request.max_concurrency is not None:
        await _validate_max_concurrency(
            request.max_concurrency, user.selected_organization_id
        )

    # Build update kwargs
    update_kwargs = {}

    if request.name is not None:
        update_kwargs["name"] = request.name

    if request.retry_config is not None:
        update_kwargs["retry_config"] = request.retry_config.model_dump()

    # Merge max_concurrency and schedule_config into orchestrator_metadata
    metadata = campaign.orchestrator_metadata or {}
    metadata_changed = False

    if request.max_concurrency is not None:
        metadata["max_concurrency"] = request.max_concurrency
        metadata_changed = True

    if request.schedule_config is not None:
        metadata["schedule_config"] = request.schedule_config.model_dump()
        metadata_changed = True

    if request.circuit_breaker is not None:
        metadata["circuit_breaker"] = request.circuit_breaker.model_dump()
        metadata_changed = True

    if request.calling_hours is not None:
        # pop-when-absent keeps the three keys mutually consistent: switching
        # from mode="custom" back to "inherit"/"off" must actually clear the
        # stale start/end, not leave them where a later write could resurrect
        # them. (The create path needs no equivalent — it starts empty.)
        ch = request.calling_hours.model_dump()
        metadata["calling_hours_mode"] = ch["mode"]
        if ch.get("start"):
            metadata["calling_hours_start"] = ch["start"]
        else:
            metadata.pop("calling_hours_start", None)
        if ch.get("end"):
            metadata["calling_hours_end"] = ch["end"]
        else:
            metadata.pop("calling_hours_end", None)
        if ch.get("off_acknowledged_at"):
            metadata["calling_hours_off_acknowledged_at"] = ch[
                "off_acknowledged_at"
            ].isoformat()
        else:
            metadata.pop("calling_hours_off_acknowledged_at", None)
        metadata_changed = True

    if metadata_changed:
        update_kwargs["orchestrator_metadata"] = metadata

    if request.workflow_id is not None:
        if not user.is_superuser:
            # Unlike create_campaign (which validates via get_workflow_name),
            # this write path had no ownership check at all — a client could
            # repoint an existing campaign at another org's workflow/agent.
            workflow_name = await db_client.get_workflow_name(
                request.workflow_id, organization_id=user.selected_organization_id
            )
            if not workflow_name:
                raise HTTPException(status_code=404, detail="Workflow not found")
        update_kwargs["workflow_id"] = request.workflow_id

    if request.telephony_configuration_id is not None:
        if user.is_superuser:
            cfg = await db_client.get_telephony_configuration(
                request.telephony_configuration_id
            )
        else:
            cfg = await db_client.get_telephony_configuration_for_org(
                request.telephony_configuration_id, user.selected_organization_id
            )
            if not cfg:
                # Client orgs may also reference the platform admin's
                # shared/managed telephony config (e.g. "Sysevo Managed") —
                # same get_platform_organization_id() pattern already used by
                # ai_providers.py/voice_library.py for other shared platform
                # resources. This must stay org-scoped to the platform org
                # specifically: the previous unscoped get_telephony_configuration(id)
                # fallback let any org reference ANY other org's telephony
                # config, including its encrypted provider credentials.
                platform_org_id = await db_client.get_platform_organization_id()
                if platform_org_id is not None:
                    cfg = await db_client.get_telephony_configuration_for_org(
                        request.telephony_configuration_id, platform_org_id
                    )
        if not cfg:
            raise HTTPException(
                status_code=400, detail="telephony_configuration_not_found"
            )
        update_kwargs["telephony_configuration_id"] = request.telephony_configuration_id
        logger.info(
            f"campaign {campaign_id}: setting telephony_configuration_id="
            f"{request.telephony_configuration_id} (cfg={cfg.name!r})"
        )

    if update_kwargs:
        await db_client.update_campaign(campaign_id=campaign_id, **update_kwargs)

    # Re-fetch to return updated data
    campaign = await db_client.get_campaign(campaign_id, org_id)
    workflow_name = await db_client.get_workflow_name(
        campaign.workflow_id, organization_id=campaign.organization_id
    )

    executed, total = await _get_campaign_stats(campaign.id)
    cfg_name = await _get_telephony_configuration_name(
        campaign.telephony_configuration_id, campaign.organization_id
    )
    return _build_campaign_response(
        campaign,
        workflow_name or "Unknown",
        executed,
        total,
        telephony_configuration_name=cfg_name,
    )


@router.get("/{campaign_id}/runs")
async def get_campaign_runs(
    campaign_id: int,
    page: int = 1,
    limit: int = 50,
    filters: Optional[str] = Query(None, description="JSON-encoded filter criteria"),
    sort_by: Optional[str] = Query(
        None, description="Field to sort by (e.g., 'duration', 'created_at')"
    ),
    sort_order: Optional[str] = Query(
        "desc", description="Sort order ('asc' or 'desc')"
    ),
    user: UserModel = Depends(get_user),
) -> CampaignRunsResponse:
    """Get campaign workflow runs with pagination, filters and sorting"""
    offset = (page - 1) * limit

    # Parse filters if provided
    filter_criteria = []
    if filters:
        try:
            filter_criteria = json.loads(filters)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid filter format")

        # Restrict allowed filter attributes for regular users
        allowed_attributes = {
            "dateRange",
            "dispositionCode",
            "duration",
            "status",
            "tokenUsage",
        }
        for filter_item in filter_criteria:
            attribute = filter_item.get("attribute")
            if attribute and attribute not in allowed_attributes:
                raise HTTPException(
                    status_code=403, detail=f"Invalid attribute '{attribute}'"
                )

    if user.is_superuser:
        campaign_for_org = await db_client.get_campaign_by_id(campaign_id)
        if not campaign_for_org:
            raise HTTPException(status_code=404, detail="Campaign not found")
        org_id_for_runs = campaign_for_org.organization_id
    else:
        org_id_for_runs = user.selected_organization_id

    try:
        runs, total_count = await db_client.get_campaign_runs_paginated(
            campaign_id,
            org_id_for_runs,
            limit=limit,
            offset=offset,
            filters=filter_criteria if filter_criteria else None,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    total_pages = (total_count + limit - 1) // limit

    return CampaignRunsResponse(
        runs=[run.model_dump() for run in runs],
        total_count=total_count,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


@router.get("/{campaign_id}/contacts")
async def get_campaign_contacts(
    campaign_id: int,
    page: int = 1,
    limit: int = 50,
    user: UserModel = Depends(get_user),
):
    """Return all queued contacts for a campaign with their latest call outcome."""
    org_id = None if user.is_superuser else user.selected_organization_id
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    offset = (page - 1) * limit
    contacts, total_count = await db_client.get_campaign_contacts_paginated(
        campaign_id, limit=limit, offset=offset
    )
    total_pages = max(1, (total_count + limit - 1) // limit)
    return {
        "contacts": contacts,
        "total_count": total_count,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


class RedialCampaignRequest(BaseModel):
    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="Name for the redial campaign"
    )
    retry_on_voicemail: bool = True
    retry_on_no_answer: bool = True
    retry_on_busy: bool = True
    retry_on_failed: bool = False
    retry_config: Optional[RetryConfigRequest] = None

    @model_validator(mode="after")
    def validate_at_least_one_reason(self):
        if not (
            self.retry_on_voicemail or self.retry_on_no_answer or self.retry_on_busy
            or self.retry_on_failed
        ):
            raise ValueError(
                "At least one of retry_on_voicemail, retry_on_no_answer, "
                "retry_on_busy, retry_on_failed must be true"
            )
        return self


@router.post("/{campaign_id}/redial")
async def redial_campaign(
    campaign_id: int,
    request: RedialCampaignRequest,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Create a new campaign that re-dials unique subscribers from a completed
    campaign whose latest call resulted in voicemail, no-answer, or busy.

    The new campaign is created in 'created' state with queued_runs pre-seeded
    from the parent's original initial contexts. A campaign can be redialed at
    most once.
    """
    org_id = None if user.is_superuser else user.selected_organization_id
    parent = await db_client.get_campaign(campaign_id, org_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if parent.state not in ("completed", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Only completed or failed campaigns can be redialed (current state: {parent.state})",
        )

    parent_meta = parent.orchestrator_metadata or {}
    if parent_meta.get("redialed_campaign_id"):
        raise HTTPException(
            status_code=400,
            detail="This campaign has already been redialed",
        )

    candidates = await db_client.get_redial_candidates(
        campaign_id=parent.id,
        include_voicemail=request.retry_on_voicemail,
        include_no_answer=request.retry_on_no_answer,
        include_busy=request.retry_on_busy,
        include_failed=request.retry_on_failed,
    )
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="No subscribers match the selected redial criteria",
        )

    queued_runs_data = [
        {
            "campaign_id": 0,  # replaced inside create_redial_campaign
            "source_uuid": c["source_uuid"],
            "context_variables": c["context_variables"],
            "state": "queued",
        }
        for c in candidates
    ]

    retry_config = (
        request.retry_config.model_dump()
        if request.retry_config
        else parent.retry_config
    )
    new_name = request.name or f"{parent.name} (Redial)"

    try:
        child = await db_client.create_redial_campaign(
            parent_campaign=parent,
            new_name=new_name,
            retry_config=retry_config,
            queued_runs_data=queued_runs_data,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    workflow_name = await db_client.get_workflow_name(
        child.workflow_id, organization_id=child.organization_id
    )
    executed, total = await _get_campaign_stats(child.id)
    cfg_name = await _get_telephony_configuration_name(
        child.telephony_configuration_id, child.organization_id
    )
    return _build_campaign_response(
        child,
        workflow_name or "Unknown",
        executed,
        total,
        telephony_configuration_name=cfg_name,
    )


@router.post("/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignResponse:
    """Resume a paused campaign"""
    org_id = None if user.is_superuser else user.selected_organization_id

    # Load campaign first so we can check telephony against the campaign's org.
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Block resume if the campaign's org has no telephony configuration.
    configs = await db_client.list_telephony_configurations(campaign.organization_id)
    if not configs:
        raise HTTPException(
            status_code=401,
            detail="You must configure telephony first by going to APP_URL/configure-telephony",
        )

    # Check Dograh quota before resuming campaign (apply per-workflow
    # model_overrides so we evaluate the keys this campaign will use).
    quota_result = await check_dograh_quota(user, workflow_id=campaign.workflow_id)
    if not quota_result.has_quota:
        raise HTTPException(status_code=402, detail=quota_result.error_message)

    # Resume the campaign using the runner service
    try:
        await campaign_runner_service.resume_campaign(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get updated campaign
    campaign = await db_client.get_campaign(campaign_id, org_id)
    workflow_name = await db_client.get_workflow_name(
        campaign.workflow_id, organization_id=campaign.organization_id
    )

    executed, total = await _get_campaign_stats(campaign.id)
    cfg_name = await _get_telephony_configuration_name(
        campaign.telephony_configuration_id, campaign.organization_id
    )
    return _build_campaign_response(
        campaign,
        workflow_name or "Unknown",
        executed,
        total,
        telephony_configuration_name=cfg_name,
    )


@router.get("/{campaign_id}/progress")
async def get_campaign_progress(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignProgressResponse:
    """Get current campaign progress and statistics"""
    # Verify campaign exists and belongs to organization
    campaign = await db_client.get_campaign(campaign_id, user.selected_organization_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Get progress from runner service
    try:
        progress = await campaign_runner_service.get_campaign_status(campaign_id)
        return CampaignProgressResponse(**progress)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class CampaignSourceDownloadResponse(BaseModel):
    download_url: str
    expires_in: int


@router.get("/{campaign_id}/source-download-url")
async def get_campaign_source_download_url(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> CampaignSourceDownloadResponse:
    """Get presigned download URL for campaign CSV source file
    Validates that the campaign belongs to the user's organization for security.
    """
    # Verify campaign exists and belongs to organization
    campaign = await db_client.get_campaign(campaign_id, user.selected_organization_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Only generate download URL for CSV files
    if campaign.source_type != "csv":
        raise HTTPException(
            status_code=400,
            detail=f"Download URL only available for CSV sources. This campaign uses {campaign.source_type}",
        )

    # Verify the file key belongs to the user's organization
    # File key format: campaigns/{org_id}/{uuid}_{filename}.csv
    if not campaign.source_id.startswith(f"campaigns/{user.selected_organization_id}/"):
        raise HTTPException(
            status_code=403,
            detail="Access denied: Source file does not belong to your organization",
        )

    # Generate presigned download URL
    try:
        download_url = await storage_fs.aget_signed_url(
            campaign.source_id,
            expiration=3600,  # 1 hour
        )

        if not download_url:
            raise HTTPException(
                status_code=500, detail="Failed to generate download URL"
            )

        return CampaignSourceDownloadResponse(
            download_url=download_url, expires_in=3600
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate download URL: {str(e)}"
        )


@router.get("/{campaign_id}/report")
async def download_campaign_report(
    campaign_id: int,
    user: UserModel = Depends(get_user),
    start_date: Optional[datetime] = Query(
        None, description="Filter runs created on or after this datetime (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter runs created on or before this datetime (ISO 8601)"
    ),
) -> StreamingResponse:
    """Download a CSV report of completed campaign runs."""
    campaign = await db_client.get_campaign(campaign_id, user.selected_organization_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    output, filename = await generate_campaign_report_csv(
        campaign_id, start_date=start_date, end_date=end_date
    )

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _do_delete_campaign(campaign_id: int, user: UserModel) -> dict:
    """Shared logic for both DELETE and POST /delete endpoints."""
    try:
        if user.is_superuser:
            _ref = await db_client.get_campaign_by_id(campaign_id)
            if not _ref:
                raise HTTPException(status_code=404, detail="Campaign not found")
            deleted = await db_client.delete_campaign(campaign_id, _ref.organization_id)
        else:
            deleted = await db_client.delete_campaign(campaign_id, user.selected_organization_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"status": "deleted", "campaign_id": campaign_id}


@router.delete("/{campaign_id}")
async def delete_campaign(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> dict:
    """Delete a campaign permanently (DELETE method)."""
    return await _do_delete_campaign(campaign_id, user)


@router.post("/{campaign_id}/delete")
async def delete_campaign_post(
    campaign_id: int,
    user: UserModel = Depends(get_user),
) -> dict:
    """Delete a campaign permanently (POST fallback for proxies that block DELETE)."""
    return await _do_delete_campaign(campaign_id, user)


@router.post("/{campaign_id}/enqueue")
async def enqueue_run(
    campaign_id: int,
    request: EnqueueRunRequest,
    user: UserModel = Depends(get_user),
):
    """Add a single scheduled run to an existing campaign.

    Exists for callbacks: campaigns are otherwise built wholesale from a source
    file, and there is no way to add one contact for one future moment.
    """
    # Org-scope the campaign. Per api/AGENTS.md an id from the request body
    # never implies ownership — fetch with the caller's org and 404 otherwise.
    # NOTE: db_client.get_campaign_by_id has no organization_id parameter (it is
    # an explicitly internal/unscoped lookup); the org-scoped fetcher used
    # elsewhere in this file (see get_campaign, start_campaign) is
    # db_client.get_campaign(campaign_id, organization_id).
    org_id = None if user.is_superuser else user.selected_organization_id
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency: a re-analysed call must not schedule the same callback twice.
    existing = await db_client.get_queued_run_by_source_uuid(
        campaign_id=campaign_id, source_uuid=request.source_uuid
    )
    if existing:
        return {"queued_run_id": existing.id, "already_enqueued": True}

    queued_run = await db_client.create_queued_run(
        campaign_id=campaign_id,
        source_uuid=request.source_uuid,
        context_variables=request.context_variables,
        scheduled_for=request.scheduled_for,
        retry_reason=request.retry_reason,
    )
    return {"queued_run_id": queued_run.id, "already_enqueued": False}


@router.patch("/{campaign_id}/queued-runs/{queued_run_id}")
async def update_queued_run_endpoint(
    campaign_id: int,
    queued_run_id: int,
    request: UpdateQueuedRunRequest,
    user: UserModel = Depends(get_user),
):
    """Reschedule or cancel a queued run. Exists for the callbacks surface."""
    # Org-scope through the campaign, exactly as enqueue_run does: an id in the
    # path never implies ownership.
    org_id = None if user.is_superuser else user.selected_organization_id
    campaign = await db_client.get_campaign(campaign_id, org_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    queued_run = await db_client.get_queued_run_by_id(queued_run_id)
    # The run must belong to THIS campaign, or the check above is decorative and
    # any org could modify any queued run by guessing an id.
    if not queued_run or queued_run.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Queued run not found")

    if queued_run.state not in ("queued", "failed"):
        # processing/processed means the call is in flight or already made:
        # rescheduling would either double-dial or rewrite history.
        raise HTTPException(
            status_code=409,
            detail=f"Cannot modify a run in state '{queued_run.state}'",
        )

    updates: dict = {}
    if request.cancel:
        # queued_run_state has no 'cancelled' member, and adding one is a
        # migration for a single UI affordance. 'failed' plus an explicit reason
        # keeps it out of the dispatcher and readable in History.
        updates["state"] = "failed"
        updates["retry_reason"] = "cancelled_by_user"
        updates["scheduled_for"] = None
    elif request.reschedule:
        updates["scheduled_for"] = request.scheduled_for
        if queued_run.state == "failed":
            # Re-opening a previously cancelled callback.
            updates["state"] = "queued"
    else:
        raise HTTPException(status_code=400, detail="Nothing to update")

    updated = await db_client.update_queued_run(queued_run_id, **updates)
    return {
        "id": updated.id,
        "state": updated.state,
        "scheduled_for": updated.scheduled_for.isoformat() if updated.scheduled_for else None,
        "retry_reason": updated.retry_reason,
    }
