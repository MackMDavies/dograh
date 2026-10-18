"""Vonage telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
import os
import re
from typing import Literal, Optional
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id
from pydantic import BaseModel, Field

from api.db import db_client
from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.dialer.compliance import calling_hours_result, phone_block_reason
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.providers.twilio.dialer_call_log import (
    create_dialer_call,
    update_dialer_call_status,
)
from api.services.telephony.providers.twilio.dialer_number_assignment import (
    DialerNumberAssignmentUnavailable,
    resolve_assigned_dialer_number,
)
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)

router = APIRouter()


async def _verify_dialer_webhook(request: Request) -> None:
    """Accept dialer webhooks only when signed for the configured application."""
    try:
        saved = await db_client.get_platform_vonage_dialer_credentials()
    except Exception as exc:  # noqa: BLE001 - fail closed if credentials can't be verified
        raise HTTPException(status_code=503, detail="Could not verify Vonage webhook settings.") from exc
    saved = saved or {}
    api_key = (saved.get("api_key") or os.getenv("VONAGE_DIALER_API_KEY") or "").strip()
    signature_secret = saved.get("signature_secret") or os.getenv("VONAGE_DIALER_SIGNATURE_SECRET") or ""
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if not api_key or not signature_secret or not token:
        raise HTTPException(status_code=401, detail="Invalid Vonage webhook authorization.")
    try:
        claims = jwt.decode(
            token,
            signature_secret,
            algorithms=["HS256"],
            options={"require": ["api_key", "iat", "exp"]},
            leeway=60,
        )
    except Exception as exc:  # noqa: BLE001 - reject any malformed/unsigned webhook
        raise HTTPException(status_code=401, detail="Invalid Vonage webhook signature.") from exc
    if claims.get("api_key") != api_key:
        raise HTTPException(status_code=401, detail="Vonage webhook is not for this application.")


@router.get("/vonage/dialer/answer", include_in_schema=False)
async def handle_dialer_answer(request: Request):
    """Return the NCCO for a rep's Vonage Client SDK PSTN call."""
    await _verify_dialer_webhook(request)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Vonage call metadata is invalid.")
    custom_data = payload.get("custom_data")
    if isinstance(custom_data, str):
        try:
            custom_data = json.loads(custom_data)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Vonage call metadata is invalid.")
    if not isinstance(custom_data, dict):
        custom_data = {}
    to_number = str(custom_data.get("to") or payload.get("to") or "")
    from_number = str(custom_data.get("from") or payload.get("from") or "")
    if not re.fullmatch(r"\+?[1-9]\d{7,14}", to_number):
        raise HTTPException(status_code=422, detail="Vonage call destination is invalid.")
    if not re.fullmatch(r"\+?[1-9]\d{7,14}", from_number):
        raise HTTPException(status_code=422, detail="Vonage caller ID is invalid.")
    return [{
        "action": "connect",
        "from": from_number.removeprefix("+"),
        "endpoint": [{"type": "phone", "number": to_number.removeprefix("+")}],
    }]


class VonageDialerCallStartRequest(BaseModel):
    call_id: UUID
    to_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    entry_id: UUID | None = None


class VonageDialerCallStatusRequest(BaseModel):
    call_id: UUID
    status: Literal["ringing", "answered", "completed", "failed", "no-answer"]
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)


@router.post("/dialer/vonage/calls/start", status_code=201)
async def start_vonage_dialer_call(
    body: VonageDialerCallStartRequest,
    user=Depends(require_sales_dialer_role),
):
    """Persist and enforce compliance before the Vonage SDK originates a call."""
    try:
        assigned = await resolve_assigned_dialer_number(user.id)
    except DialerNumberAssignmentUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not verify your assigned dialer number; no call was placed.",
        ) from exc
    if not assigned or assigned.get("provider") != "vonage":
        raise HTTPException(status_code=409, detail="This rep has no active Vonage number assigned.")
    if assigned.get("direction") not in {"outbound", "both"}:
        raise HTTPException(status_code=409, detail="The assigned Vonage number is not enabled for outbound calls.")
    if not user.provider_id:
        raise HTTPException(status_code=503, detail="Rep account is missing its Supabase user mapping.")

    block_reason = await phone_block_reason(body.to_number)
    if block_reason:
        raise HTTPException(status_code=403, detail=f"Call blocked by compliance check ({block_reason}).")
    allowed, reason, phone_us_state = await calling_hours_result(
        body.to_number, str(body.entry_id) if body.entry_id else None
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=f"Call blocked by calling-hours check ({reason}).")

    created = await create_dialer_call(
        parent_call_sid=str(body.call_id),
        rep_user_id=user.provider_id,
        entry_id=str(body.entry_id) if body.entry_id else None,
        from_number=str(assigned["phone_number"]),
        to_number=body.to_number,
        provider="vonage",
        phone_us_state=phone_us_state,
    )
    if not created:
        raise HTTPException(status_code=503, detail="Could not save this call to dialer history; no call was placed.")
    return {
        "call_id": str(body.call_id),
        "status": "initiated",
        "from_number": str(assigned["phone_number"]),
    }


@router.post("/dialer/vonage/calls/status", status_code=204)
async def update_vonage_dialer_call_status(
    body: VonageDialerCallStatusRequest,
    user=Depends(require_sales_dialer_role),
):
    if not user.provider_id:
        raise HTTPException(status_code=503, detail="Rep account is missing its Supabase user mapping.")
    await update_dialer_call_status(
        parent_call_sid=str(body.call_id),
        child_call_sid=None,
        status="in-progress" if body.status == "answered" else body.status,
        duration_seconds=body.duration_seconds,
        rep_user_id=user.provider_id,
        provider="vonage",
    )
    return None


@router.get("/ncco", include_in_schema=False)
async def handle_ncco_webhook(
    workflow_id: int,
    user_id: int,
    workflow_run_id: int,
    organization_id: Optional[int] = None,
):
    """Handle NCCO (Nexmo Call Control Objects) webhook for Vonage.

    Returns JSON response instead of XML like TwiML.
    """

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    provider = await get_telephony_provider_for_run(
        workflow_run, organization_id or user_id
    )

    response_content = await provider.get_webhook_response(
        workflow_id, user_id, workflow_run_id
    )

    return json.loads(response_content)


@router.post("/vonage/events/{workflow_run_id}")
async def handle_vonage_events(
    request: Request,
    workflow_run_id: int,
):
    """Handle Vonage-specific event webhooks.

    Vonage sends all call events to a single endpoint.
    Events include: started, ringing, answered, complete, failed, etc.
    """
    set_current_run_id(workflow_run_id)
    # Parse the event data
    event_data = await request.json()
    logger.info(f"[run {workflow_run_id}] Received Vonage event: {event_data}")

    # Get workflow run for processing
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.error(f"[run {workflow_run_id}] Workflow run not found")
        return {"status": "error", "message": "Workflow run not found"}

    # For a completed call that includes cost info, capture it immediately
    if event_data.get("status") == "completed":
        # Vonage sometimes includes price info in the webhook
        if "price" in event_data or "rate" in event_data:
            try:
                if workflow_run.cost_info:
                    # Store immediate cost info if available
                    cost_info = workflow_run.cost_info.copy()
                    if "price" in event_data:
                        cost_info["vonage_webhook_price"] = float(event_data["price"])
                    if "rate" in event_data:
                        cost_info["vonage_webhook_rate"] = float(event_data["rate"])
                    if "duration" in event_data:
                        cost_info["vonage_webhook_duration"] = int(
                            event_data["duration"]
                        )

                    await db_client.update_workflow_run(
                        run_id=workflow_run_id, cost_info=cost_info
                    )
                    logger.info(
                        f"[run {workflow_run_id}] Captured Vonage cost info from webhook"
                    )
            except Exception as e:
                logger.error(
                    f"[run {workflow_run_id}] Failed to capture Vonage cost from webhook: {e}"
                )

    # Get workflow and provider
    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.error(f"[run {workflow_run_id}] Workflow not found")
        return {"status": "error", "message": "Workflow not found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    # Parse the event data into generic format
    parsed_data = provider.parse_status_callback(event_data)

    # Create StatusCallbackRequest from parsed data
    status_update = StatusCallbackRequest(
        call_id=parsed_data["call_id"],
        status=parsed_data["status"],
        from_number=parsed_data.get("from_number"),
        to_number=parsed_data.get("to_number"),
        direction=parsed_data.get("direction"),
        duration=parsed_data.get("duration"),
        extra=parsed_data.get("extra", {}),
    )

    # Process the status update
    await _process_status_update(workflow_run_id, status_update)

    # Return 204 No Content as expected by Vonage
    return {"status": "ok"}
