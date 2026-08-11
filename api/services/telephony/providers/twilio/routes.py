"""Twilio telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id
from pydantic import BaseModel
from starlette.responses import HTMLResponse
from twilio.request_validator import RequestValidator

from api.db import db_client
from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.providers.twilio.dialer_number_assignment import (
    resolve_assigned_caller_id,
)
from api.services.telephony.providers.twilio.voice_sdk import (
    VoiceSdkNotConfigured,
    generate_voice_access_token,
)
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)

router = APIRouter()


class VoiceTokenResponse(BaseModel):
    token: str
    identity: str


@router.get("/voice-token")
async def get_voice_token(
    user=Depends(require_sales_dialer_role),
) -> VoiceTokenResponse:
    identity = f"rep-{user.id}"
    try:
        token = generate_voice_access_token(identity)
    except VoiceSdkNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return VoiceTokenResponse(token=token, identity=identity)


async def _verify_twilio_signature(request: Request, form_data: dict) -> bool:
    auth_token = os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")
    signature = request.headers.get("x-twilio-signature", "")
    if not auth_token or not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(str(request.url), form_data, signature)


@router.post("/voice-connect", include_in_schema=False)
async def handle_voice_connect(request: Request):
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on voice-connect webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    to_number = form_data.get("To", "").strip()
    caller_id = (
        await resolve_assigned_caller_id(form_data.get("From", ""))
        or os.environ.get("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "")
    )
    if not to_number or not caller_id:
        logger.error("voice-connect missing To number or SYSEVO_TWILIO_DEFAULT_CALLER_ID")
        return HTMLResponse(content=hangup, media_type="application/xml")

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Dial callerId="{caller_id}"><Number>{to_number}</Number></Dial></Response>'
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.post("/twiml", include_in_schema=False)
async def handle_twiml_webhook(
    workflow_id: int,
    user_id: int,
    workflow_run_id: int,
    organization_id: int,
    request: Request,
):
    """
    Handle initial webhook from telephony provider.
    Returns provider-specific response (e.g., TwiML for Twilio).
    Never returns a non-200 response — Twilio plays an error message if we do.
    """
    _hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    try:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        if not workflow_run:
            logger.error(f"[run {workflow_run_id}] Workflow run not found for TwiML webhook")
            return HTMLResponse(content=_hangup, media_type="application/xml")

        provider = await get_telephony_provider_for_run(workflow_run, organization_id)
        callback_data = dict(await request.form())

        is_valid = await provider.verify_inbound_signature(
            str(request.url),
            callback_data,
            dict(request.headers),
        )
        if not is_valid:
            logger.warning(
                f"[run {workflow_run_id}] Invalid Twilio signature on answer webhook"
            )
            return HTMLResponse(content=_hangup, media_type="application/xml")

        response_content = await provider.get_webhook_response(
            workflow_id, user_id, workflow_run_id
        )
        return HTMLResponse(content=response_content, media_type="application/xml")

    except Exception as exc:
        logger.error(f"[run {workflow_run_id}] TwiML webhook error: {exc}")
        return HTMLResponse(content=_hangup, media_type="application/xml")


@router.post("/twilio/status-callback/{workflow_run_id}")
async def handle_twilio_status_callback(
    workflow_run_id: int,
    request: Request,
):
    """Handle Twilio-specific status callbacks."""
    set_current_run_id(workflow_run_id)

    # Parse form data
    form_data = await request.form()
    callback_data = dict(form_data)

    logger.info(
        f"[run {workflow_run_id}] Received status callback: {json.dumps(callback_data)}"
    )

    # Get workflow run to find organization
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(f"Workflow run {workflow_run_id} not found for status callback")
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    # Get workflow and provider
    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"Workflow {workflow_run.workflow_id} not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    is_valid = await provider.verify_inbound_signature(
        str(request.url),
        callback_data,
        dict(request.headers),
    )
    if not is_valid:
        logger.warning(f"Invalid webhook signature for workflow run {workflow_run_id}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Parse the callback data into generic format
    parsed_data = provider.parse_status_callback(callback_data)

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

    return {"status": "success"}
