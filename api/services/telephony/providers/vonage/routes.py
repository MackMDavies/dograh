"""Vonage telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
import os
import re
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id

from api.db import db_client
from api.services.telephony.factory import get_telephony_provider_for_run
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
