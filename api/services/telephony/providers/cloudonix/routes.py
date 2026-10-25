"""Cloudonix telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json

from fastapi import APIRouter, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id

from api.db import db_client
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)

router = APIRouter()


@router.post("/cloudonix/status-callback/{workflow_run_id}")
async def handle_cloudonix_status_callback(
    workflow_run_id: int,
    request: Request,
):
    """Handle Cloudonix-specific status callbacks.

    Cloudonix sends call status updates to the callback URL specified during call initiation.
    """
    set_current_run_id(workflow_run_id)
    # Parse callback data - determine if JSON or form data
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        callback_data = await request.json()
    else:
        # Assume form data (like Twilio)
        form_data = await request.form()
        callback_data = dict(form_data)

    logger.info(
        f"[run {workflow_run_id}] Received Cloudonix status callback: {json.dumps(callback_data)}"
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
        str(request.url), callback_data, dict(request.headers)
    )
    if not is_valid:
        logger.warning(
            f"[run {workflow_run_id}] Invalid Cloudonix webhook signature on status callback"
        )
        return {"status": "error", "reason": "invalid_signature"}

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


@router.post("/cloudonix/cdr")
async def handle_cloudonix_cdr(request: Request):
    """Handle Cloudonix CDR (Call Detail Record) webhooks.

    Cloudonix sends CDR records when calls complete. The CDR contains:
    - domain: Used to identify the organization
    - call_id: Used to find the workflow run
    - disposition: Call termination status (ANSWER, BUSY, CANCEL, FAILED, CONGESTION, NOANSWER)
    - duration/billsec: Call duration information
    """
    try:
        cdr_data = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse Cloudonix CDR JSON: {e}")
        return {"status": "error", "message": "Invalid JSON payload"}

    # Extract domain to find organization
    domain = cdr_data.get("domain")
    if not domain:
        logger.warning("Cloudonix CDR missing domain field")
        return {"status": "error", "message": "Missing domain field"}

    # Extract call_id to find workflow run
    call_id = cdr_data.get("session").get("token")
    logger.info(f"Cloudonix CDR data for call id {call_id} - {cdr_data}")
    if not call_id:
        logger.warning("Cloudonix CDR missing call_id field")
        return {"status": "error", "message": "Missing call_id field"}

    # Find workflow run by call_id in gathered_context
    workflow_run = await db_client.get_workflow_run_by_call_id(call_id)
    if not workflow_run:
        logger.warning(f"No workflow run found for Cloudonix call_id: {call_id}")
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    workflow_run_id = workflow_run.id
    set_current_run_id(workflow_run_id)
    logger.info(f"[run {workflow_run_id}] Processing Cloudonix CDR for call {call_id}")

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"[run {workflow_run_id}] Workflow {workflow_run.workflow_id} not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )
    is_valid = await provider.verify_inbound_signature(
        str(request.url), cdr_data, dict(request.headers)
    )
    if not is_valid:
        logger.warning(f"[run {workflow_run_id}] Invalid Cloudonix webhook signature on CDR")
        return {"status": "error", "reason": "invalid_signature"}

    # Convert CDR to status update using StatusCallbackRequest
    status_update = StatusCallbackRequest.from_cloudonix_cdr(cdr_data)

    # Process the status update
    await _process_status_update(workflow_run_id, status_update)

    logger.info(
        f"[run {workflow_run_id}] Cloudonix CDR processed successfully - "
        f"disposition: {cdr_data.get('disposition')}, status: {status_update.status}"
    )

    return {"status": "success"}


# ── Transfers (Keypad & Native Phone Tools) ───────────────────────────────────────
# CXML is Twilio-compatible, so the colleague's leg uses the same TwiML as Twilio's
# transfers. transfer_id is an unguessable uuid4 known only to Cloudonix and Redis.


def _cxml(body: str):
    from fastapi import Response

    return Response(content=body, media_type="application/xml")


@router.post("/cloudonix/transfer-answer/{transfer_id}", include_in_schema=False)
async def cloudonix_transfer_answer(transfer_id: str):
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager
    from api.services.telephony.warm_transfer import (
        report_transfer_leg,
        twilio_accept_twiml,
        twilio_decline_twiml,
        twilio_transfer_twiml,
    )
    from api.utils.common import get_backend_endpoints

    context = await (await get_call_transfer_manager()).get_transfer_context(transfer_id)
    if context is None or context.resolved:
        return _cxml(twilio_decline_twiml())
    await report_transfer_leg(transfer_id, "answered")
    if context.warm and context.introduction:
        backend, _ = await get_backend_endpoints()
        return _cxml(twilio_transfer_twiml(
            context.conference_name, context.introduction,
            f"{backend}/api/v1/telephony/cloudonix/transfer-accept/{transfer_id}",
        ))
    return _cxml(twilio_accept_twiml(context.conference_name))


@router.post("/cloudonix/transfer-accept/{transfer_id}", include_in_schema=False)
async def cloudonix_transfer_accept(transfer_id: str, request: Request):
    from api.services.telephony.warm_transfer import (
        report_transfer_accept,
        twilio_accept_twiml,
        twilio_decline_twiml,
    )

    form = dict(await request.form())
    conference = await report_transfer_accept(transfer_id, str(form.get("Digits") or ""))
    return _cxml(twilio_accept_twiml(conference) if conference else twilio_decline_twiml())


@router.post("/cloudonix/after-stream/{workflow_run_id}", include_in_schema=False)
async def cloudonix_after_stream(workflow_run_id: int):
    """The caller's CXML continues here when the agent's stream ends. A connected
    transfer joins them to the colleague; otherwise the call simply ends."""
    from api.db import db_client
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager

    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    run = await db_client.get_workflow_run_by_id(workflow_run_id)
    call_id = (run.gathered_context or {}).get("call_id") if run else None
    if not call_id:
        return _cxml(hangup)
    manager = await get_call_transfer_manager()
    transfer = await manager.find_transfer_context_for_call(call_id)
    if transfer is None or not transfer.accepted:
        return _cxml(hangup)
    await manager.remove_transfer_context(transfer.transfer_id)
    return _cxml(
        '<?xml version="1.0" encoding="UTF-8"?><Response><Dial>'
        f'<Conference endConferenceOnExit="true">{transfer.conference_name}</Conference>'
        "</Dial></Response>"
    )
