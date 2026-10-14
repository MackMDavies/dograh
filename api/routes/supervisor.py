"""Supervisor controls for a live AI-agent call (portal: Voice AI > Live now).

Thin routes; the mechanism lives in services/supervisor/live_control.py.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.enums import WorkflowRunMode, WorkflowRunState
from api.services.auth.depends import get_user
from api.services.supervisor.live_control import (
    SupervisorCommandError,
    is_controllable,
    publish_command,
    validate_command,
)

router = APIRouter(prefix="/supervisor", tags=["supervisor"])


class SupervisorCommand(BaseModel):
    type: Literal["whisper", "takeover"]
    text: Optional[str] = None
    destination: Optional[str] = None


async def _live_run(run_id: int, user: UserModel):
    run = await db_client.get_workflow_run(
        run_id,
        organization_id=None if user.is_superuser else user.selected_organization_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Call not found")
    return run


def _is_running(run) -> bool:
    return run.state == WorkflowRunState.RUNNING.value and not run.is_completed


@router.get("/runs/{run_id}")
async def supervisor_status(run_id: int, user: UserModel = Depends(get_user)):
    """Whether Whisper / Take over can reach this call right now."""
    run = await _live_run(run_id, user)
    live = _is_running(run) and await is_controllable(run_id)
    phone = run.mode not in (
        WorkflowRunMode.WEBRTC.value,
        WorkflowRunMode.SMALLWEBRTC.value,
        WorkflowRunMode.TEXTCHAT.value,
    )
    return {"controllable": live, "whisper": live, "takeover": live and phone}


@router.post("/runs/{run_id}")
async def supervisor_command(
    run_id: int, body: SupervisorCommand, user: UserModel = Depends(get_user)
):
    run = await _live_run(run_id, user)
    if not _is_running(run):
        raise HTTPException(status_code=409, detail="This call has ended")
    try:
        command = validate_command(body.model_dump())
    except SupervisorCommandError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not await is_controllable(run_id) or await publish_command(run_id, command) == 0:
        raise HTTPException(
            status_code=409, detail="This call can't take supervisor controls right now"
        )
    return {"ok": True, "type": command["type"]}
