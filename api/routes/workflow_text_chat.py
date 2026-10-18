import os
from datetime import datetime
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pipecat.utils.run_context import set_current_run_id
from pydantic import BaseModel, Field

from api.db import db_client
from api.db.models import UserModel, WorkflowRunTextSessionModel
from api.enums import WorkflowRunMode
from api.services.auth.depends import get_user
from api.services.quota_service import check_dograh_quota
from api.services.workflow.test_sandbox_binding import (
    SandboxWorkflowNotFoundError,
    make_sandbox_workflow_binding,
    resolve_requested_sandbox_definition,
    validate_sandbox_test_identity,
)
from api.services.workflow.test_sandbox_mode import (
    SANDBOX_ANNOTATION,
    build_run_annotations,
    sandbox_secret_matches,
)
from api.services.workflow.text_chat_session_service import (
    TextChatPendingTurnLostError,
    TextChatSessionExecutionError,
    TextChatSessionRevisionConflictError,
    TextChatTurnNotFoundError,
    append_text_chat_user_message,
    default_text_chat_checkpoint,
    default_text_chat_session_data,
    execute_pending_text_chat_turn,
    initialize_text_chat_session,
    normalize_text_chat_checkpoint,
    normalize_text_chat_session_data,
    rewind_text_chat_session_state,
)

router = APIRouter(prefix="/workflow", tags=["workflow-text-chat"])


class CreateTextChatSessionRequest(BaseModel):
    name: str | None = None
    initial_context: Dict[str, Any] | None = None
    annotations: Dict[str, Any] | None = None
    sandbox_mode: bool = False
    published_version_number: int | None = Field(default=None, ge=1)
    candidate_draft_version_number: int | None = Field(default=None, ge=1)
    expected_workflow_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class AppendTextChatMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int | None = None


class RewindTextChatSessionRequest(BaseModel):
    cursor_turn_id: str | None = None
    expected_revision: int | None = None


class WorkflowRunTextSessionResponse(BaseModel):
    workflow_run_id: int
    workflow_id: int
    name: str
    mode: str
    state: str
    is_completed: bool
    revision: int
    initial_context: Dict[str, Any] | None = None
    gathered_context: Dict[str, Any] | None = None
    annotations: Dict[str, Any] | None = None
    session_data: Dict[str, Any]
    checkpoint: Dict[str, Any]
    created_at: datetime
    updated_at: datetime | None = None


def _get_state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def _build_response(
    text_session: WorkflowRunTextSessionModel,
) -> WorkflowRunTextSessionResponse:
    workflow_run = text_session.workflow_run
    return WorkflowRunTextSessionResponse(
        workflow_run_id=workflow_run.id,
        workflow_id=workflow_run.workflow_id,
        name=workflow_run.name,
        mode=workflow_run.mode,
        state=_get_state_value(workflow_run.state),
        is_completed=workflow_run.is_completed,
        revision=text_session.revision,
        initial_context=workflow_run.initial_context,
        gathered_context=workflow_run.gathered_context,
        annotations=workflow_run.annotations,
        session_data=normalize_text_chat_session_data(text_session.session_data),
        checkpoint=normalize_text_chat_checkpoint(text_session.checkpoint),
        created_at=text_session.created_at,
        updated_at=text_session.updated_at,
    )


def _revision_conflict_detail(e: Any) -> dict[str, Any]:
    return {
        "message": "Text chat session revision conflict",
        "expected_revision": e.expected_revision,
        "actual_revision": e.actual_revision,
    }


def _require_selected_organization_id(user: UserModel) -> int:
    if user.selected_organization_id is None:
        raise HTTPException(status_code=403, detail="Organization context is required")
    return user.selected_organization_id


async def _ensure_text_chat_quota(user: UserModel, workflow_id: int) -> None:
    quota_result = await check_dograh_quota(user, workflow_id=workflow_id)
    if not quota_result.has_quota:
        raise HTTPException(status_code=402, detail=quota_result.error_message)


async def _load_text_session_or_404(
    workflow_id: int,
    run_id: int,
    user: UserModel,
) -> WorkflowRunTextSessionModel:
    set_current_run_id(run_id)
    organization_id = _require_selected_organization_id(user)
    text_session = await db_client.get_workflow_run_text_session(
        run_id, organization_id=organization_id
    )
    if not text_session or not text_session.workflow_run:
        raise HTTPException(status_code=404, detail="Text chat session not found")
    if text_session.workflow_run.workflow_id != workflow_id:
        raise HTTPException(status_code=404, detail="Text chat session not found")
    if text_session.workflow_run.mode != WorkflowRunMode.TEXTCHAT.value:
        raise HTTPException(
            status_code=400, detail="Workflow run is not a text chat session"
        )
    return text_session


async def _execute_pending_turn_response(
    *,
    workflow_id: int,
    run_id: int,
    text_session: WorkflowRunTextSessionModel,
) -> WorkflowRunTextSessionResponse:
    try:
        updated_text_session = await execute_pending_text_chat_turn(
            workflow_id=workflow_id,
            run_id=run_id,
            text_session=text_session,
        )
    except TextChatSessionRevisionConflictError as e:
        raise HTTPException(status_code=409, detail=_revision_conflict_detail(e))
    except TextChatPendingTurnLostError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except TextChatSessionExecutionError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _build_response(updated_text_session)


@router.post(
    "/{workflow_id}/text-chat/sessions",
    response_model=WorkflowRunTextSessionResponse,
)
async def create_text_chat_session(
    workflow_id: int,
    request: CreateTextChatSessionRequest,
    user: UserModel = Depends(get_user),
    x_sysevo_secret: str | None = Header(None, alias="x-sysevo-secret"),
) -> WorkflowRunTextSessionResponse:
    sandbox_authorized = False
    bound_definition = None
    if request.sandbox_mode:
        expected_secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
        if not sandbox_secret_matches(x_sysevo_secret, expected_secret):
            raise HTTPException(
                status_code=403, detail="Sandbox test authorization required"
            )
        test_run_id = (request.annotations or {}).get("agent_test_run_id")
        scenario_id = (request.annotations or {}).get("scenario_id")
        if (
            not isinstance(test_run_id, str)
            or len(test_run_id) != 36
            or not isinstance(scenario_id, str)
        ):
            raise HTTPException(
                status_code=400,
                detail="Sandbox test run and scenario identifiers are required",
            )
        try:
            validate_sandbox_test_identity(test_run_id, scenario_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if (request.published_version_number is None) == (
            request.candidate_draft_version_number is None
        ):
            raise HTTPException(
                status_code=400,
                detail="Sandbox test sessions must bind to exactly one published or candidate draft version",
            )
        sandbox_authorized = True
    elif (
        request.published_version_number is not None
        or request.candidate_draft_version_number is not None
        or request.expected_workflow_hash is not None
    ):
        raise HTTPException(
            status_code=403,
            detail="Published version pinning is reserved for authenticated sandbox tests",
        )

    organization_id = _require_selected_organization_id(user)
    if sandbox_authorized:
        try:
            bound_definition = await resolve_requested_sandbox_definition(
                db_client,
                workflow_id=workflow_id,
                organization_id=organization_id,
                version_number=request.published_version_number
                or request.candidate_draft_version_number,
                version_kind="draft"
                if request.candidate_draft_version_number is not None
                else "published",
                expected_hash=request.expected_workflow_hash,
            )
        except SandboxWorkflowNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _ensure_text_chat_quota(user, workflow_id)

    session_name = request.name or f"WR-TEXT-{uuid4().hex[:6].upper()}"
    try:
        workflow_run = await db_client.create_workflow_run(
            name=session_name,
            workflow_id=workflow_id,
            mode=WorkflowRunMode.TEXTCHAT.value,
            user_id=user.id,
            initial_context=request.initial_context,
            use_draft=not sandbox_authorized,
            definition_version_number=(
                request.published_version_number
                or request.candidate_draft_version_number
            )
            if sandbox_authorized
            else None,
            definition_version_status="draft"
            if request.candidate_draft_version_number is not None
            else "published",
            organization_id=organization_id,
        )
    except ValueError as e:
        status_code = 409 if "no longer available" in str(e) else 404
        raise HTTPException(status_code=status_code, detail=str(e))

    set_current_run_id(workflow_run.id)

    annotations = build_run_annotations(
        {
            "tester": {
                "source": "workflow_editor",
                "modality": "text",
            }
        },
        request.annotations,
        sandbox_authorized=sandbox_authorized,
    )
    if bound_definition is not None:
        annotations[SANDBOX_ANNOTATION]["workflow_binding"] = (
            make_sandbox_workflow_binding(
                bound_definition,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.id,
                run_id=request.annotations.get("agent_test_run_id"),
                scenario_id=request.annotations.get("scenario_id"),
                expected_hash=request.expected_workflow_hash,
                signing_secret=expected_secret,
            )
        )
    workflow_run = await db_client.update_workflow_run(
        workflow_run.id,
        annotations=annotations,
    )

    text_session = await db_client.ensure_workflow_run_text_session(
        workflow_run.id,
        session_data=default_text_chat_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )

    try:
        text_session = await initialize_text_chat_session(
            run_id=workflow_run.id,
            text_session=text_session,
        )
    except TextChatSessionRevisionConflictError as e:
        raise HTTPException(status_code=409, detail=_revision_conflict_detail(e))

    return await _execute_pending_turn_response(
        workflow_id=workflow_id,
        run_id=workflow_run.id,
        text_session=text_session,
    )


@router.get(
    "/{workflow_id}/text-chat/sessions/{run_id}",
    response_model=WorkflowRunTextSessionResponse,
)
async def get_text_chat_session(
    workflow_id: int,
    run_id: int,
    user: UserModel = Depends(get_user),
) -> WorkflowRunTextSessionResponse:
    text_session = await _load_text_session_or_404(workflow_id, run_id, user)
    return _build_response(text_session)


@router.post(
    "/{workflow_id}/text-chat/sessions/{run_id}/messages",
    response_model=WorkflowRunTextSessionResponse,
)
async def append_text_chat_message(
    workflow_id: int,
    run_id: int,
    request: AppendTextChatMessageRequest,
    user: UserModel = Depends(get_user),
) -> WorkflowRunTextSessionResponse:
    text_session = await _load_text_session_or_404(workflow_id, run_id, user)
    await _ensure_text_chat_quota(user, workflow_id)

    try:
        text_session = await append_text_chat_user_message(
            run_id=run_id,
            text_session=text_session,
            user_text=request.text,
            expected_revision=request.expected_revision,
        )
    except TextChatSessionRevisionConflictError as e:
        raise HTTPException(status_code=409, detail=_revision_conflict_detail(e))

    return await _execute_pending_turn_response(
        workflow_id=workflow_id,
        run_id=run_id,
        text_session=text_session,
    )


@router.post(
    "/{workflow_id}/text-chat/sessions/{run_id}/rewind",
    response_model=WorkflowRunTextSessionResponse,
)
async def rewind_text_chat_session(
    workflow_id: int,
    run_id: int,
    request: RewindTextChatSessionRequest,
    user: UserModel = Depends(get_user),
) -> WorkflowRunTextSessionResponse:
    text_session = await _load_text_session_or_404(workflow_id, run_id, user)
    try:
        text_session = await rewind_text_chat_session_state(
            run_id=run_id,
            text_session=text_session,
            cursor_turn_id=request.cursor_turn_id,
            expected_revision=request.expected_revision,
        )
    except TextChatTurnNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TextChatSessionRevisionConflictError as e:
        raise HTTPException(status_code=409, detail=_revision_conflict_detail(e))

    return _build_response(text_session)
