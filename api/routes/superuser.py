import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_superuser
from api.services.auth.stack_auth import stackauth

router = APIRouter(prefix="/superuser", tags=["superuser"])


class ImpersonateRequest(BaseModel):
    """Request payload for superadmin impersonation.

    Either ``provider_user_id`` **or** ``user_id`` must be supplied. If both are
    provided, ``provider_user_id`` takes precedence.
    """

    provider_user_id: str | None = None
    user_id: int | None = None


class ImpersonateResponse(BaseModel):
    refresh_token: str
    access_token: str


class SuperuserWorkflowRunResponse(BaseModel):
    id: int
    name: str
    workflow_id: int
    workflow_name: Optional[str]
    user_id: Optional[int]
    organization_id: Optional[int]
    organization_name: Optional[str]
    mode: str
    is_completed: bool
    recording_url: Optional[str]
    transcript_url: Optional[str]
    usage_info: Optional[dict]
    cost_info: Optional[dict]
    initial_context: Optional[dict]
    gathered_context: Optional[dict]
    created_at: datetime


class SuperuserWorkflowRunsListResponse(BaseModel):
    workflow_runs: List[SuperuserWorkflowRunResponse]
    total_count: int
    page: int
    limit: int
    total_pages: int


class SuperuserWorkflowPhoneNumber(BaseModel):
    id: int
    address: str
    address_normalized: str


class SuperuserWorkflowItem(BaseModel):
    id: int
    name: str
    status: str
    created_at: datetime
    folder_id: Optional[int]
    workflow_uuid: Optional[str]
    organization_id: Optional[int]
    total_runs: int
    phone_numbers: List[SuperuserWorkflowPhoneNumber] = []


@router.get("/workflows", response_model=List[SuperuserWorkflowItem])
async def list_all_workflows(
    organization_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None, description="Comma-separated: active,archived"),
    user: UserModel = Depends(get_superuser),
) -> List[SuperuserWorkflowItem]:
    """List all workflows across all organizations. Superuser only."""
    workflows = await db_client.get_all_workflows_for_superuser(
        organization_id=organization_id,
        status=status,
    )
    workflow_ids = [w["id"] for w in workflows]
    phone_numbers = await db_client.list_phone_numbers_for_workflows(workflow_ids)
    phone_map: dict[int, list] = {}
    for pn in phone_numbers:
        phone_map.setdefault(pn.inbound_workflow_id, []).append(pn)

    return [
        SuperuserWorkflowItem(
            **w,
            phone_numbers=[
                SuperuserWorkflowPhoneNumber(
                    id=pn.id,
                    address=pn.address,
                    address_normalized=pn.address_normalized,
                )
                for pn in phone_map.get(w["id"], [])
            ],
        )
        for w in workflows
    ]


class SuperuserCampaignItem(BaseModel):
    id: int
    name: str
    state: str
    organization_id: Optional[int]
    workflow_id: int
    workflow_name: Optional[str]
    total_rows: Optional[int]
    processed_rows: int
    failed_rows: int
    executed_count: int
    total_queued_count: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


@router.get("/campaigns", response_model=List[SuperuserCampaignItem])
async def list_all_campaigns(
    organization_id: Optional[int] = Query(None),
    user: UserModel = Depends(get_superuser),
) -> List[SuperuserCampaignItem]:
    """List campaigns across all organizations. Superuser only."""
    campaigns = await db_client.get_all_campaigns_for_superuser(organization_id=organization_id)

    # Resolve workflow names across all orgs
    workflow_ids = list({c.workflow_id for c in campaigns})
    workflow_map: dict[int, str] = {}
    if workflow_ids:
        workflows = await db_client.get_workflows_by_ids_superuser(workflow_ids)
        workflow_map = {w["id"]: w["name"] for w in workflows}

    # Fetch live execution stats from queued_runs so executed_count / total_queued_count
    # match what the regular /campaign/ endpoint returns.
    campaign_ids = [c.id for c in campaigns]
    stats_map: dict[int, dict] = {}
    if campaign_ids:
        stats_map = await db_client.get_queued_runs_stats_for_campaigns(campaign_ids)

    return [
        SuperuserCampaignItem(
            id=c.id,
            name=c.name,
            state=c.state,
            organization_id=c.organization_id,
            workflow_id=c.workflow_id,
            workflow_name=workflow_map.get(c.workflow_id),
            total_rows=c.total_rows,
            processed_rows=c.processed_rows,
            failed_rows=c.failed_rows,
            executed_count=stats_map.get(c.id, {}).get("executed", 0),
            total_queued_count=stats_map.get(c.id, {}).get("total", 0),
            created_at=c.created_at,
            started_at=c.started_at,
            completed_at=c.completed_at,
        )
        for c in campaigns
    ]


@router.get("/organizations", response_model=List[dict])
async def list_organizations(
    user: UserModel = Depends(get_superuser),
) -> List[dict]:
    """List all organizations. Superuser only."""
    orgs = await db_client.get_all_organizations()
    return orgs


@router.post("/impersonate")
async def impersonate(
    request: ImpersonateRequest, user: UserModel = Depends(get_superuser)
) -> ImpersonateResponse:
    """Impersonate a user as a super-admin.
    Internally, Stack Auth requires the **provider user ID** (a UUID-ish string)
    to create an impersonation session.
    """

    provider_user_id: str | None = request.provider_user_id

    # ------------------------------------------------------------------
    # Fallback: resolve provider_user_id from internal ``user_id``
    # ------------------------------------------------------------------
    if provider_user_id is None:
        if request.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either 'provider_user_id' or 'user_id' must be provided.",
            )

        db_user = await db_client.get_user_by_id(request.user_id)

        if db_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {request.user_id} not found.",
            )

        provider_user_id = db_user.provider_id

    # ------------------------------------------------------------------
    # Call Stack Auth to create the impersonation session
    # ------------------------------------------------------------------
    session = await stackauth.impersonate(provider_user_id)

    return ImpersonateResponse(
        refresh_token=session["refresh_token"],
        access_token=session["access_token"],
    )


@router.get("/workflow-runs")
async def get_workflow_runs(
    page: int = Query(1, ge=1, description="Page number (starts from 1)"),
    limit: int = Query(50, ge=1, le=100, description="Number of items per page"),
    filters: Optional[str] = Query(None, description="JSON-encoded filter criteria"),
    sort_by: Optional[str] = Query(
        None, description="Field to sort by (e.g., 'duration', 'created_at')"
    ),
    sort_order: Optional[str] = Query(
        "desc", description="Sort order ('asc' or 'desc')"
    ),
    user: UserModel = Depends(get_superuser),
) -> SuperuserWorkflowRunsListResponse:
    """
    Get paginated list of all workflow runs with organization information.
    Requires superuser privileges.

    Filters should be provided as a JSON-encoded array of filter criteria.
    Example: [{"field": "id", "type": "number", "value": {"value": 680}}]
    """
    offset = (page - 1) * limit

    # Parse filters if provided
    filter_criteria = None
    if filters:
        try:
            filter_criteria = json.loads(filters)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid filter format")

    # Validate sort_order
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    workflow_runs, total_count = await db_client.get_workflow_runs_for_superadmin(
        limit=limit,
        offset=offset,
        filters=filter_criteria,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = (total_count + limit - 1) // limit  # Ceiling division

    return SuperuserWorkflowRunsListResponse(
        workflow_runs=[SuperuserWorkflowRunResponse(**run) for run in workflow_runs],
        total_count=total_count,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )
