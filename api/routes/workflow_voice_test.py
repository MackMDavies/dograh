"""Authenticated entry point for an already reserved private voice session."""

import asyncio
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.workflow.test_sandbox_mode import sandbox_secret_matches
from api.services.workflow.voice_test_control import (
    VoiceControlClient,
    VoiceControlError,
    positive_int,
)
from api.services.workflow.voice_test_job import (
    VoiceJobContext,
    enqueue_voice_job,
    read_voice_job_status,
)

router = APIRouter(prefix="/workflow")
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class VoiceTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    lease_token: str = Field(
        min_length=36, max_length=36, pattern=UUID_PATTERN, repr=False
    )


@router.get("/test-voice/capabilities")
async def voice_test_capabilities(
    user: Annotated[UserModel, Depends(get_user)],
    x_sysevo_secret: Annotated[str | None, Header(alias="x-sysevo-secret")] = None,
):
    if not sandbox_secret_matches(
        x_sysevo_secret, os.getenv("SYSEVO_MEMORY_SECRET", "")
    ):
        raise HTTPException(
            status_code=403, detail="Internal sandbox authorization required"
        )
    if not positive_int(user.selected_organization_id) or not positive_int(user.id):
        raise HTTPException(
            status_code=400, detail="An authenticated organization is required"
        )
    from api.services.workflow.voice_test_capacity import private_voice_capabilities
    from api.tasks.arq import get_arq_redis

    try:
        async with asyncio.timeout(5):
            return await private_voice_capabilities(await get_arq_redis())
    except Exception:  # noqa: BLE001 - never expose Redis pool or connection errors
        raise HTTPException(
            status_code=503, detail="voice_worker_unavailable"
        ) from None


@router.post("/{workflow_id}/test-voice/sessions", status_code=202)
async def create_voice_test_session(
    workflow_id: Annotated[int, Path(gt=0)],
    request: VoiceTestRequest,
    user: Annotated[UserModel, Depends(get_user)],
    x_sysevo_secret: Annotated[str | None, Header(alias="x-sysevo-secret")] = None,
):
    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    if not sandbox_secret_matches(x_sysevo_secret, secret):
        raise HTTPException(
            status_code=403, detail="Internal sandbox authorization required"
        )
    if not positive_int(user.selected_organization_id) or not positive_int(user.id):
        raise HTTPException(
            status_code=400, detail="An authenticated organization is required"
        )
    # Import the pool lazily; importing worker registration must not run a job.
    from api.tasks.arq import get_arq_redis

    context = VoiceJobContext(
        request.session_id,
        request.lease_token,
        workflow_id,
        user.selected_organization_id,
        user.id,
    )
    try:
        async with httpx.AsyncClient() as http:
            return await enqueue_voice_job(
                db_client,
                VoiceControlClient(http, secret),
                await get_arq_redis(),
                context,
            )
    except VoiceControlError as exc:
        raise HTTPException(
            status_code=exc.status_code or 409, detail=exc.code
        ) from None
    except Exception:  # noqa: BLE001 - redact arbitrary SDK/connection errors at the HTTP boundary
        raise HTTPException(status_code=503, detail="voice_queue_unavailable") from None


@router.post("/{workflow_id}/test-voice/status")
async def voice_test_session_status(
    workflow_id: Annotated[int, Path(gt=0)],
    request: VoiceTestRequest,
    user: Annotated[UserModel, Depends(get_user)],
    x_sysevo_secret: Annotated[str | None, Header(alias="x-sysevo-secret")] = None,
):
    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    if not sandbox_secret_matches(x_sysevo_secret, secret):
        raise HTTPException(
            status_code=403, detail="Internal sandbox authorization required"
        )
    if not positive_int(user.selected_organization_id) or not positive_int(user.id):
        raise HTTPException(
            status_code=400, detail="An authenticated organization is required"
        )
    from api.tasks.arq import get_arq_redis

    context = VoiceJobContext(
        request.session_id,
        request.lease_token,
        workflow_id,
        user.selected_organization_id,
        user.id,
    )
    try:
        async with httpx.AsyncClient() as http:
            return await read_voice_job_status(
                db_client,
                VoiceControlClient(http, secret),
                await get_arq_redis(),
                context,
                secret,
            )
    except VoiceControlError as exc:
        raise HTTPException(
            status_code=exc.status_code or 409, detail=exc.code
        ) from None
    except Exception:  # noqa: BLE001 - never return raw private report or database errors
        raise HTTPException(
            status_code=503, detail="voice_report_unavailable"
        ) from None
