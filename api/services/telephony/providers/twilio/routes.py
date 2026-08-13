"""Twilio telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
import os
import re
from urllib.parse import urlencode
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
from api.services.telephony.providers.twilio.dialer_call_listeners import (
    create_dialer_call_listener,
)
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
    is_manager_or_admin,
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

# Twilio Call SID grammar: literal "CA" + 32 lowercase hex. Used to validate
# the one caller-supplied value that becomes a conference name - see
# _serve_listen_in_twiml.
_TWILIO_CALL_SID_PATTERN = re.compile(r"^CA[0-9a-f]{32}$")


class VoiceTokenResponse(BaseModel):
    token: str
    identity: str


@router.get("/voice-token")
async def get_voice_token(
    user=Depends(require_sales_dialer_role),
) -> VoiceTokenResponse:
    identity = f"rep-{user.id}"
    try:
        token = await generate_voice_access_token(identity)
    except VoiceSdkNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return VoiceTokenResponse(token=token, identity=identity)


async def _resolve_dialer_auth_token() -> str | None:
    db_token = await db_client.get_platform_dialer_auth_token()
    if db_token:
        return db_token
    return os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")


async def _resolve_dialer_caller_id() -> str | None:
    creds = await db_client.get_platform_dialer_credentials()
    if creds and creds.get("default_caller_id"):
        return creds["default_caller_id"]
    return os.environ.get("SYSEVO_TWILIO_DEFAULT_CALLER_ID")


async def _verify_twilio_signature(request: Request, form_data: dict) -> bool:
    auth_token = await _resolve_dialer_auth_token()
    signature = request.headers.get("x-twilio-signature", "")
    if not auth_token or not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(str(request.url), form_data, signature)


def _conference_telemetry_attributes(*, events_url: str, recording_url: str) -> str:
    """The conference-level attributes that must be identical on EVERY leg.

    Twilio applies conference-level attributes only from the TwiML of the
    participant that CREATES the conference, and ignores them on everyone
    who joins afterwards. The rep normally creates it - calls.create returns
    as soon as the lead's call is queued, and the lead still has to ring -
    but "normally" is a race, not a guarantee: an instantly-answering
    voicemail, a SIP endpoint, or an unusually slow rep leg can put the lead
    in first. If that happens and only the rep's leg carried these, the
    conference comes up with no recording, no recording callback and no
    conference-start event at all - which also leaves conference_sid NULL,
    so update_dialer_call_recording_by_conference_sid would have nothing to
    match even if a recording did appear. So both legs carry them, from this
    one definition, and whichever leg wins the race the conference is
    recorded and reporting.

    Repeating them cannot double-record: recording is a property of the
    conference, not of a participant, so only the creator's copy takes
    effect and the loser's is discarded.

    Returns "" (attributes omitted entirely) unless BOTH URLs are absolute.
    That is the guard for an unresolved backend endpoint, which yields a
    relative "/api/v1/..." rather than an empty string: Twilio rejects a
    relative callback URL, and a rejected TwiML document costs the whole
    leg - silence for the lead - which is strictly worse than a conference
    that merely isn't reporting.
    """
    if not events_url.startswith(("http://", "https://")):
        return ""
    if not recording_url.startswith(("http://", "https://")):
        return ""
    return (
        f"statusCallback={quoteattr(events_url)} "
        'statusCallbackEvent="start end join leave" '
        'record="record-from-start" '
        f"recordingStatusCallback={quoteattr(recording_url)} "
    )


@router.post("/voice-connect", include_in_schema=False)
async def handle_voice_connect(request: Request):
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on voice-connect webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    # Listen-in arrives HERE, not at /dialer-listen-connect, and this branch
    # is the only reason the feature works at all.
    #
    # generate_voice_access_token grants a VoiceGrant over one
    # TWILIO_TWIML_APP_SID, a TwiML App has exactly one Voice URL, and that
    # URL is this endpoint. device.connect() can pass arbitrary custom params
    # but cannot pick a different URL - so a manager's listen-in leg lands on
    # voice-connect with a ListenParentCallSid and no To, and would otherwise
    # fall straight through to the "missing To number" hangup below.
    #
    # Routing only, never an authorization shortcut: _serve_listen_in_twiml
    # runs its own full is_manager_or_admin check, so a sales_rep who adds
    # this param to their own dial is rejected there. The signature has
    # already been verified once, above, and the parsed form is handed over
    # rather than re-read - the body is consumed and cannot be read twice.
    #
    # Placed before every other field read so the rep dial path - the hot
    # path on every sales call - is untouched when the param is absent: one
    # dict lookup against a falsy default.
    if str(form_data.get("ListenParentCallSid", "")).strip():
        return await _serve_listen_in_twiml(form_data)

    to_number = form_data.get("To", "").strip()
    raw_from = form_data.get("From", "")
    entry_id = form_data.get("EntryId", "").strip() or None
    parent_call_sid = form_data.get("CallSid", "")
    # Three-tier resolution, most specific first:
    #   1. the number assigned to THIS rep (dialer_phone_numbers), then
    #   2. the active platform Twilio account's default_caller_id from the
    #      platform_twilio_credentials table, then
    #   3. the SYSEVO_TWILIO_DEFAULT_CALLER_ID env var.
    # Tiers 2 and 3 are both inside _resolve_dialer_caller_id, which is what
    # makes the per-account default apply here as well as to signature
    # verification; tier 1 stays in front of it so per-rep assignment is not
    # lost when an account-level default exists (it always does in prod).
    caller_id = (
        await resolve_assigned_caller_id(raw_from)
        or await _resolve_dialer_caller_id()
        or ""
    )
    if not to_number or not caller_id or not parent_call_sid:
        logger.error(
            "voice-connect missing To number, a resolvable default caller id, or CallSid"
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
    #
    # events_url/recording_url are handed to the lead's leg so it can repeat
    # the conference-level attributes verbatim - see
    # _conference_telemetry_attributes on why every leg needs them. They're
    # passed rather than re-resolved in that handler for two reasons: the
    # lead is on the line waiting for that TwiML, and get_backend_endpoints()
    # can block on a cloudflared lookup; and resolving once here is what
    # makes the two legs' attributes identical by construction instead of by
    # coincidence. Query params are covered by Twilio's signature (validated
    # against the full request URL), and this endpoint already trusts a far
    # more sensitive signed param in conference_name - which decides which
    # live call you are placed into.
    #
    # urlencode, not concatenation: these values are absolute URLs whose "/"
    # and ":" would otherwise land raw in a query string.
    lead_join_url = (
        f"{backend_endpoint}/api/v1/telephony/dialer-conference-join?"
        + urlencode(
            {
                "conference_name": conference_name,
                "muted": "false",
                "end_on_exit": "true",
                "start_on_enter": "true",
                "events_url": conference_events_url,
                "recording_url": recording_callback_url,
            }
        )
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
        "<Conference "
        + _conference_telemetry_attributes(
            events_url=conference_events_url, recording_url=recording_callback_url
        )
        + 'startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">'
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
    events_url: str = "",
    recording_url: str = "",
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

    # voice-connect passes these so this leg can repeat the conference-level
    # attributes - see _conference_telemetry_attributes. Absent (or not
    # absolute) they're simply omitted, which is exactly how this leg behaved
    # before, so an older in-flight join URL during a deploy still works.
    telemetry_attributes = _conference_telemetry_attributes(
        events_url=events_url, recording_url=recording_url
    )
    if not telemetry_attributes:
        logger.warning(
            f"dialer-conference-join for '{conference_name}' has no usable callback URLs - "
            "this leg carries no conference recording/status attributes, so if it creates "
            "the conference there will be no recording and no conference-start event"
        )

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
        f"<Conference {telemetry_attributes}muted={quoteattr(muted)} "
        f"endConferenceOnExit={quoteattr(end_on_exit)} "
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
    # unhandled: dialer-listen-connect records a manager's listen session
    # itself, so nothing on this branch needs participant presence. They stay
    # subscribed for whenever something does.
    if event_type == "conference-start" and conference_sid and parent_call_sid:
        await update_dialer_call_conference_sid(
            parent_call_sid=parent_call_sid, conference_sid=conference_sid
        )
    elif event_type == "conference-end" and parent_call_sid:
        await _cancel_orphaned_lead_leg(parent_call_sid)

    return {"status": "success"}


async def _serve_listen_in_twiml(form_data: dict) -> HTMLResponse:
    """Place a manager/admin into a live call's conference as a MUTED listener.

    Takes already-parsed, already-signature-verified form data, because it has
    TWO entry points (see handle_voice_connect's delegation and
    handle_dialer_listen_connect below) and re-reading `request.form()` after
    the body has been consumed once would yield nothing. Verifying the
    signature is the caller's job, done exactly once, at the route.

    Everything about the returned TwiML is chosen so the two people already on
    the call cannot tell anyone joined:

    - muted="true"                  - the manager can never be heard.
    - beep="false"                  - Twilio's DEFAULT is beep="true", which
      would play a join tone to BOTH the rep and the lead and announce the
      silent monitor out loud. This attribute is the whole feature.
    - startConferenceOnEnter="false" - joining a conference that has already
      ended must not resurrect it.
    - endConferenceOnExit="false"   - the manager hanging up must not end the
      call they were monitoring.

    Unlike the pure status-callback endpoints (dialer-conference-events,
    dialer-call-status, dialer-recording-callback) every failure path here
    returns hangup TwiML rather than raising 401/500: there is a live call
    leg waiting on this response, and a non-200 makes Twilio read out its own
    error message - to the manager, on a leg that is about to join a call
    they are supposed to be monitoring silently.

    No _conference_telemetry_attributes here, deliberately. Those exist so
    whichever of the rep/lead legs creates the conference makes it recorded
    and reporting; the manager joins an already-created conference, so their
    copy would be discarded anyway - and resolving them costs a
    get_backend_endpoints() call (which can block on a cloudflared lookup) on
    a path where a human is waiting to hear a live call. The residual race -
    a manager whose leg somehow creates the conference first, costing the
    recording - is accepted knowingly and is on the deployment checklist to
    confirm.
    """
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    # Blanket try, on this function's own terms rather than by delegation.
    # create_dialer_call_listener and is_manager_or_admin each swallow their
    # own errors, but db_client.get_user_by_id does NOT (a DB blip raises)
    # and neither does the string handling around them - and code BETWEEN two
    # individually-protected calls is not itself protected. An escape here
    # becomes a 500, which Twilio both reads aloud and retries.
    try:
        raw_from = form_data.get("From", "")
        parent_call_sid = str(form_data.get("ListenParentCallSid", "")).strip()
        if not parent_call_sid:
            logger.error("listen-in missing ListenParentCallSid")
            return HTMLResponse(content=hangup, media_type="application/xml")

        # Constrain the SID to Twilio's own grammar before it becomes a
        # conference name. There is no FK on dialer_call_listeners.
        # parent_call_sid and no lookup here (a fail-closed existence check in
        # front of live audio is a worse trade - see the design notes), so
        # this shape guard is what stops an authenticated manager naming an
        # arbitrary conference, and it demotes the XML escaping below from
        # sole defense to belt-and-braces. Free: a regex, no I/O.
        if not _TWILIO_CALL_SID_PATTERN.match(parent_call_sid):
            logger.warning("listen-in rejected malformed ListenParentCallSid")
            return HTMLResponse(content=hangup, media_type="application/xml")

        # Same "client:rep-{id}" Voice SDK identity a rep gets: /voice-token
        # issues that shape to everyone holding a sales dialer role, managers
        # and admins included. The helper only extracts a user id - the
        # "rep-" prefix is the token format, not a role claim - and the
        # actual authorization is is_manager_or_admin below.
        manager_id = _parse_rep_id_from_identity(raw_from)
        if manager_id is None:
            logger.warning("listen-in: unrecognized identity")
            return HTMLResponse(content=hangup, media_type="application/xml")

        user = await db_client.get_user_by_id(manager_id)
        if not user or not user.provider_id:
            logger.warning(f"listen-in: no Supabase user for dograh id {manager_id}")
            return HTMLResponse(content=hangup, media_type="application/xml")

        # Authorize BEFORE recording anything and before returning any
        # conference TwiML: an unauthorized caller must leave no
        # dialer_call_listeners row claiming they listened, and must never
        # reach the audio. is_manager_or_admin fails CLOSED, so an error
        # reaching Supabase lands here too.
        #
        # This check is why delegation from voice-connect is routing and not
        # a bypass: BOTH entry points land here, so a sales_rep who hand-
        # crafts a ListenParentCallSid param is rejected on exactly this line.
        if not await is_manager_or_admin(user.provider_id):
            logger.warning(f"listen-in: {user.provider_id} is not a manager/admin")
            return HTMLResponse(content=hangup, media_type="application/xml")

        await create_dialer_call_listener(
            parent_call_sid=parent_call_sid, manager_user_id=user.provider_id
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into a webhook handler
        logger.error(f"listen-in failed: {exc}")
        return HTMLResponse(content=hangup, media_type="application/xml")

    # escape() is belt-and-braces now that the SID shape is validated above,
    # and stays for exactly that reason - it must not be the only thing
    # standing between a caller-supplied string and the TwiML document.
    conference_name = conference_name_for(parent_call_sid)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Dial>"
        '<Conference muted="true" startConferenceOnEnter="false" '
        'endConferenceOnExit="false" beep="false">'
        f"{escape(conference_name)}</Conference>"
        "</Dial></Response>"
    )
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.post("/dialer-listen-connect", include_in_schema=False)
async def handle_dialer_listen_connect(request: Request):
    """Dedicated listen-in answer URL.

    Currently UNREACHABLE from the browser and kept deliberately: the Voice
    SDK can only dial the single Voice URL configured on the one
    TWILIO_TWIML_APP_SID that generate_voice_access_token grants, which is
    /voice-connect. A Device can pass custom params but cannot choose a
    different URL, so listen-in actually arrives via voice-connect's
    delegation. This route costs nothing, keeps the listen path directly
    addressable and testable, and means pointing a second TwiML App here
    later is a console change with no code change.
    """
    hangup = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    form_data = dict(await request.form())

    if not await _verify_twilio_signature(request, form_data):
        logger.warning("Invalid Twilio signature on dialer-listen-connect webhook")
        return HTMLResponse(content=hangup, media_type="application/xml")

    return await _serve_listen_in_twiml(form_data)


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
    # completed one. Logged rather than dropped in silence: of the statuses
    # that would then arrive (in-progress/completed/absent/failed), "failed"
    # means the operator expects audio and none exists, which is exactly the
    # kind of thing that otherwise only surfaces as a human noticing.
    if recording_status != "completed":
        logger.info(
            f"dialer-recording-callback ignoring recording {recording_sid} for conference "
            f"{conference_sid} at status '{recording_status or 'unknown'}' - not completed"
        )
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
