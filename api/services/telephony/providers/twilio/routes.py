"""Twilio telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
import os
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id
from pydantic import BaseModel
from starlette.responses import HTMLResponse
from twilio.request_validator import RequestValidator

from api.db import db_client
from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.providers.twilio.dialer_call_log import (
    create_dialer_call,
    get_dialer_call_child_leg,
    update_dialer_call_child_sid,
    update_dialer_call_conference_sid,
    update_dialer_call_recording_by_conference_sid,
    update_dialer_call_status,
)
from api.services.telephony.providers.twilio.dialer_conference import (
    cancel_call,
    conference_name_for,
    dial_lead_into_conference,
    parent_call_sid_from_conference_name,
)
from api.services.telephony.providers.twilio.dialer_number_assignment import (
    _parse_rep_id_from_identity,
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
from api.utils.common import get_backend_endpoints

router = APIRouter()

# Statuses at which issuing a cancel on the lead's leg is pointless: the leg
# either joined the conference (Twilio's own teardown ends it) or is already
# terminal. Anything else - including an unknown/missing status - is treated
# as "possibly still ringing" and cancelled; see dialer_conference.cancel_call.
#
# Deliberately a NEGATIVE set. A positive "is cancellable" set would make an
# unknown or missing status SKIP the cancel, which is the wrong way to fail
# for something that exists to prevent abandoned calls.
#
# Overlaps api.routes.telephony._TERMINAL_CALL_STATUSES - same Twilio
# vocabulary, different question ("can we stop polling?" vs "is a cancel
# pointless?"), so they are not identical and are kept separate rather than
# imported across the routes <-> provider boundary. If you edit one, read the
# other.
_LEAD_LEG_SETTLED_STATUSES = frozenset(
    {"in-progress", "answered", "completed", "busy", "no-answer", "canceled", "failed"}
)


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
    raw_from = form_data.get("From", "")
    entry_id = form_data.get("EntryId", "").strip() or None
    parent_call_sid = form_data.get("CallSid", "")
    caller_id = (
        await resolve_assigned_caller_id(raw_from)
        or os.environ.get("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "")
    )
    if not to_number or not caller_id or not parent_call_sid:
        logger.error(
            "voice-connect missing To number, SYSEVO_TWILIO_DEFAULT_CALLER_ID, or CallSid"
        )
        return HTMLResponse(content=hangup, media_type="application/xml")

    rep_id = _parse_rep_id_from_identity(raw_from)
    if rep_id is not None:
        user = await db_client.get_user_by_id(rep_id)
        if user and user.provider_id:
            await create_dialer_call(
                parent_call_sid=parent_call_sid,
                rep_user_id=user.provider_id,
                entry_id=entry_id,
                from_number=caller_id,
                to_number=to_number,
            )

    # Reuse the same publicly-reachable backend URL resolution every other
    # telephony provider webhook in this codebase uses (env var, falling
    # back to a cloudflared tunnel URL for local dev) rather than adding a
    # second, parallel "public base URL" concept.
    #
    # This is caught rather than propagated so the failure is a clean,
    # explained hangup instead of a 500 - but it IS fatal to the call now.
    # Under the old <Dial><Number> shape an empty endpoint only cost us
    # status/recording callbacks; with Conference the lead's join URL is
    # handed to Twilio's REST API, which rejects a non-absolute `url`, so
    # calls.create raises, dial_lead_into_conference returns None, and the
    # rep gets the "could not connect" message below.
    try:
        backend_endpoint, _ = await get_backend_endpoints()
    except Exception as exc:  # noqa: BLE001 - deliberate: must never break call setup
        logger.error(f"voice-connect could not resolve backend endpoint for callbacks: {exc}")
        backend_endpoint = ""

    conference_name = conference_name_for(parent_call_sid)
    conference_events_url = f"{backend_endpoint}/api/v1/telephony/dialer-conference-events"
    recording_callback_url = f"{backend_endpoint}/api/v1/telephony/dialer-recording-callback"
    # Plain URL query string, passed to Twilio's REST API as a function
    # argument rather than embedded in XML - the "&" separators are correct
    # as-is here and must NOT be XML-escaped.
    #
    # end_on_exit=true is load-bearing: the lead hanging up first is the
    # majority outcome on a cold-call dialer, and it must tear the whole
    # conference down so the rep's leg ends too. That's what fires the Voice
    # SDK's `disconnect` event, which is what makes the frontend show the
    # disposition panel. With end_on_exit=false the rep would be left alone
    # in a live conference, in silence, with a running timer and no prompt.
    lead_join_url = (
        f"{backend_endpoint}/api/v1/telephony/dialer-conference-join"
        f"?conference_name={conference_name}&muted=false&end_on_exit=true&start_on_enter=true"
    )
    lead_status_callback_url = (
        f"{backend_endpoint}/api/v1/telephony/dialer-call-status"
        f"?parent_call_sid={parent_call_sid}"
    )

    # Dial the lead into the same conference the rep is about to join.
    # dial_lead_into_conference fails soft (logs, returns None) rather than
    # raising - but None means no lead leg exists at all (missing creds or a
    # rejected/failed calls.create), never a partial success. Putting the rep
    # into a conference nobody will ever join would just be silence and a
    # running timer, so say what happened and hang up instead.
    child_call_sid = await dial_lead_into_conference(
        parent_call_sid=parent_call_sid,
        lead_number=to_number,
        caller_id=caller_id,
        join_conference_url=lead_join_url,
        status_callback_url=lead_status_callback_url,
    )
    if not child_call_sid:
        logger.error(f"voice-connect failed to dial lead into conference for {parent_call_sid}")
        # create_dialer_call already inserted this row as "initiated" above,
        # and the ONLY thing that ever moves it on is the dialer-call-status
        # webhook - whose URL is attached solely to the lead's leg, which was
        # never created. Without this the row is stranded at "initiated"
        # forever. (A no-op PATCH matching zero rows is fine on the path
        # where no row was created because the rep wasn't recognized.)
        await update_dialer_call_status(
            parent_call_sid=parent_call_sid,
            child_call_sid=None,
            status="failed",
            duration_seconds=0,
        )
        return HTMLResponse(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?><Response>'
                "<Say>We could not connect that call. Please try again.</Say>"
                "<Hangup/></Response>"
            ),
            media_type="application/xml",
        )

    # Persist the lead leg's SID now, while we're holding it, rather than
    # waiting for the lead's own "initiated" status callback to write it.
    # The conference-end orphan cleanup needs this SID to end a still-ringing
    # lead, and a rep who hangs up immediately can beat that callback.
    await update_dialer_call_child_sid(
        parent_call_sid=parent_call_sid, child_call_sid=child_call_sid
    )

    # No <Say> on the rep's leg: the disclosure belongs on the lead's leg
    # (see handle_dialer_conference_join), both so the called party actually
    # hears it and so the rep joins sub-second instead of missing a fast
    # answerer's "Hello?" behind ~4s of speech.
    # beep="false" everywhere - the old <Dial><Number> bridge had no join
    # tones, and a later task has a manager joining silently to listen.
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Dial>"
        f"<Conference statusCallback={quoteattr(conference_events_url)} "
        'statusCallbackEvent="start end join leave" '
        'startConferenceOnEnter="true" endConferenceOnExit="true" beep="false" '
        'record="record-from-start" '
        f"recordingStatusCallback={quoteattr(recording_callback_url)}>"
        f"{escape(conference_name)}</Conference>"
        "</Dial>"
        "</Response>"
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.post("/dialer-conference-join", include_in_schema=False)
async def handle_dialer_conference_join(
    request: Request,
    conference_name: str = "",
    muted: str = "false",
    end_on_exit: str = "true",
    start_on_enter: str = "true",
):
    """Default end_on_exit="true" is deliberately the fail-SAFE default, even
    though voice-connect (its only caller) always passes it explicitly. If
    that param were ever dropped in transit, "false" would resurrect a nasty
    bug: the lead hangs up, the conference stays alive with the rep alone,
    the Voice SDK never fires `disconnect`, and the rep's UI sits there with
    a running timer and no disposition prompt. A listen-in leg that must NOT
    end the conference has to pass end_on_exit=false explicitly."""
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-conference-join webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    if not conference_name:
        logger.error("dialer-conference-join missing conference_name")
        return HTMLResponse(content=hangup, media_type="application/xml")

    # The recording/monitoring disclosure lives here, on the lead's own leg,
    # rather than on the rep's leg in voice-connect - this is the leg the
    # called party is actually listening to. A manager's listen-in leg does
    # NOT come through here (it has its own dialer-listen-connect endpoint),
    # so this never announces a silent monitor.
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>This call may be recorded and monitored for quality assurance.</Say>"
        "<Dial>"
        f"<Conference muted={quoteattr(muted)} endConferenceOnExit={quoteattr(end_on_exit)} "
        f'startConferenceOnEnter={quoteattr(start_on_enter)} beep="false">'
        f"{escape(conference_name)}</Conference>"
        "</Dial></Response>"
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


async def _cancel_orphaned_lead_leg(parent_call_sid: str) -> None:
    """End the lead's leg if the conference ended before they ever joined it.

    The rep's leg carries endConferenceOnExit="true", so a rep hanging up (or
    skipping on to the next lead) ends the conference - but that only drops
    its *participants*. A lead whose phone is still ringing has not joined
    yet, so their leg survives, they answer, dialer-conference-join drops
    them into a freshly-recreated empty conference, and they hear silence:
    an abandoned call placed from our own caller ID. The old <Dial><Number>
    bridge cancelled that leg for us. See dialer_conference.cancel_call.

    Fail-soft on its own terms, not merely by delegation: the two helpers it
    calls each swallow their own errors, but the code BETWEEN them is this
    function's, and an exception there would escape into the webhook and
    become a 500 that Twilio retries. The blanket except is what makes the
    guarantee independent of either callee's future contract.
    """
    try:
        leg = await get_dialer_call_child_leg(parent_call_sid=parent_call_sid)

        if leg is None:
            # No dialer_calls row at all. Expected, not an anomaly: rows are
            # only created for a recognized rep identity, so an unrecognized
            # caller legitimately has none - and a row we never wrote can't
            # have a lead leg we need to end.
            logger.debug(
                f"dialer-conference-events: no dialer_calls row for {parent_call_sid}, "
                "nothing to clean up"
            )
            return

        child_call_sid = leg.get("child_call_sid")
        lead_status = (leg.get("status") or "").strip().lower()

        if not child_call_sid:
            # The row EXISTS but has no lead SID. voice-connect persists that
            # the moment the dial returns, so by the time a conference can end
            # it should always be there. Reaching this means the write failed
            # and the lead's leg is now unreachable by SID - a real anomaly.
            logger.error(
                f"dialer-conference-events: dialer_calls row for {parent_call_sid} has no "
                "child_call_sid, cannot end a possibly-orphaned lead leg"
            )
            return

        if lead_status in _LEAD_LEG_SETTLED_STATUSES:
            return

        logger.info(
            f"dialer-conference-events: conference ended with lead leg {child_call_sid} "
            f"at status '{lead_status or 'unknown'}' - ending orphaned leg"
        )
        await cancel_call(call_sid=child_call_sid)
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into a webhook handler
        logger.error(f"Failed orphaned lead leg cleanup for {parent_call_sid}: {exc}")


@router.post("/dialer-conference-events", include_in_schema=False)
async def handle_dialer_conference_events(request: Request):
    """Conference lifecycle callback. Unlike the TwiML-serving endpoints this
    raises 401 on a bad signature rather than returning hangup TwiML - same
    as dialer-call-status and dialer-recording-callback, which are likewise
    pure status callbacks with no live call leg waiting on a response."""
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-conference-events webhook")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = form_data.get("StatusCallbackEvent", "")
    conference_sid = form_data.get("ConferenceSid", "")
    parent_call_sid = parent_call_sid_from_conference_name(form_data.get("FriendlyName", ""))

    # An event table, not a pipeline. participant-join/leave arrive here too
    # (statusCallbackEvent is "start end join leave") and are deliberately
    # unhandled for now - Task 9 will want them for participant presence.
    if event_type == "conference-start" and conference_sid and parent_call_sid:
        await update_dialer_call_conference_sid(
            parent_call_sid=parent_call_sid, conference_sid=conference_sid
        )
    elif event_type == "conference-end" and parent_call_sid:
        await _cancel_orphaned_lead_leg(parent_call_sid)

    return {"status": "success"}


@router.post("/dialer-call-status", include_in_schema=False)
async def handle_dialer_call_status(request: Request, parent_call_sid: str = ""):
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-call-status webhook")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if not parent_call_sid:
        logger.warning("dialer-call-status webhook missing parent_call_sid query param")
        return {"status": "ignored", "reason": "missing_parent_call_sid"}

    child_call_sid = form_data.get("CallSid") or None
    call_status = form_data.get("CallStatus", "").strip().lower() or "initiated"
    raw_duration = form_data.get("CallDuration")
    duration_seconds = int(raw_duration) if raw_duration and raw_duration.isdigit() else None

    await update_dialer_call_status(
        parent_call_sid=parent_call_sid,
        child_call_sid=child_call_sid,
        status=call_status,
        duration_seconds=duration_seconds,
    )
    return {"status": "success"}


@router.post("/dialer-recording-callback", include_in_schema=False)
async def handle_dialer_recording_callback(request: Request):
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-recording-callback webhook")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # A Conference recording's RecordingStatusCallback identifies the
    # conference via ConferenceSid, not any participant's CallSid - this
    # replaced the old <Dial record> callback shape (which reported the
    # parent leg's own CallSid) when calls moved to a named Conference for
    # live-monitoring support. See
    # docs/superpowers/specs/2026-08-12-dialer-live-call-monitoring-design.md
    # in the sysevo repo.
    conference_sid = form_data.get("ConferenceSid", "")
    recording_sid = form_data.get("RecordingSid", "")
    recording_status = form_data.get("RecordingStatus", "")
    if not conference_sid or not recording_sid:
        logger.warning("dialer-recording-callback missing ConferenceSid or RecordingSid")
        return {"status": "ignored", "reason": "missing_fields"}

    # <Conference record="record-from-start"> fires this webhook only on
    # completion by default - but that's the TwiML's behavior, not this
    # handler's guarantee. If recordingStatusCallbackEvent is ever expanded
    # to include in-progress/absent events, this guard stops a premature or
    # failed recording from being written as if it were a playable
    # completed one.
    if recording_status != "completed":
        return {"status": "ignored", "reason": "recording_not_completed"}

    await update_dialer_call_recording_by_conference_sid(
        conference_sid=conference_sid, recording_sid=recording_sid
    )
    return {"status": "success"}


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
