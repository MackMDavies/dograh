"""SignalWire webhooks for the sales-rep dialer.

SCOPE - read this before adding anything here. SignalWire exists in this
codebase for the sales-rep browser softphone and NOTHING else. Campaigns,
the AI-agent pipeline (/twiml, /twilio/status-callback), managed number
provisioning, quick connect, inbound routing and per-org telephony config
are all Twilio and must stay that way.

That is also why this router is mounted DIRECTLY by
``api.routes.telephony`` instead of being registered as a ProviderSpec in
``api.services.telephony.registry``: the registry feeds ``factory.py`` and
the agent pipeline, so registering here would make SignalWire selectable for
agent calls, which is exactly what this boundary forbids.

DEFENSIVE BY DESIGN. The exact shape of SignalWire's SWML webhook payload
has not been observed on this account - no browser spike was possible before
the first live call. So every handler here:

  * logs the ENTIRE request body, prefixed ``SW-DIALER-PAYLOAD:``, so the
    real shape is learned from the first real call rather than guessed at
    again;
  * looks for each value it needs in every plausible location rather than
    one assumed path;
  * never raises. /sw-dialer-connect returns hangup SWML for anything it
    cannot handle, because a rep is on the line waiting for this response
    and a 500 is silence with no explanation.

Delete the payload logging once the shape is confirmed.
"""

import asyncio
import hmac
from contextlib import suppress
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import APIRouter, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from loguru import logger
from starlette.responses import JSONResponse

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer.swml import (
    build_conference_join_swml,
    SPOKEN_COMPANY_NAME,
    build_dialer_swml,
    build_hangup_swml,
    build_inbound_hold_swml,
    build_no_agents_swml,
)
from api.services.telephony.dialer.live_transcribe import LiveTranscriber
from api.services.telephony.dialer.tap_relay import (
    publish_frames,
    publish_text,
    subscribe_stream,
)
from api.services.telephony.dialer.inbound_call_log import (
    close_inbound_call,
    create_inbound_call,
    resolve_inbound_plan,
)
from api.services.telephony.providers.twilio.dialer_call_log import (
    create_dialer_call,
    update_dialer_call_recording_url,
    update_dialer_call_status,
)
from api.services.telephony.providers.twilio.dialer_number_assignment import (
    _parse_rep_id_from_identity,
    resolve_assigned_caller_id,
)
from api.db import db_client
from api.utils.common import get_backend_endpoints

router = APIRouter()

# E.164. Deliberately strict: SignalWire rejects anything else anyway, and a
# malformed number reaching the connect verb costs the rep a dead call with
# no error, whereas rejecting here produces a clean hangup and a log line.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Punctuation humans put in phone numbers. Stripped before validation.
_PHONE_NOISE = re.compile(r"[\s\-().]")

# Containers, in priority order, that a value might live under in the
# payload. First non-empty hit wins. Ordered most-likely-first based on how
# SWML documents pass user variables; the tail entries are cheap insurance.
_CONTAINER_PATHS: tuple[tuple[str, ...], ...] = (
    ("params",),
    ("vars", "userVariables"),
    ("call", "params"),
    ("userVariables",),
    (),  # top level
    ("call",),
    ("call", "vars", "userVariables"),
    ("vars",),
    ("variables",),
    ("data",),
)

# Deliberately does NOT include "destination": in SignalWire's vocabulary
# that is the Fabric resource address the browser dialled
# ("/public/sysevo-dialer?channel=audio"), not the lead's phone number.
_LEAD_KEYS = ("lead", "lead_number", "leadNumber", "to", "To", "to_number")
_ENTRY_KEYS = ("entry", "entry_id", "entryId", "EntryId")
_REP_KEYS = (
    "rep",
    "identity",
    "from",
    "From",
    "caller_id_name",
    "callerIdName",
    "reference",
    "subscriber_reference",
    "from_number",
)
# Set by the browser when a rep accepts an inbound call: it names the conference the
# caller is already waiting in, so this dial joins them instead of placing a new one.
_CONFERENCE_KEYS = ("conference", "conference_name", "conferenceName", "room")
_CALL_ID_KEYS = (
    "call_id",
    "callId",
    "parent_call_sid",
    "CallSid",
    "call_sid",
    "callSid",
    "id",
)
_STATE_KEYS = ("call_state", "callState", "state", "status", "CallStatus", "call_status")
_DURATION_KEYS = ("duration", "call_duration", "duration_seconds", "CallDuration")
_END_REASON_KEYS = ("end_reason", "endReason", "failed_reason", "failedReason")
_RECORDING_URL_KEYS = ("url", "recording_url", "recordingUrl", "RecordingUrl", "record_url")


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _candidates(payload: dict, query: dict, keys: tuple[str, ...]) -> list[str]:
    """Every non-empty value for any of ``keys``, in priority order.

    Containers are the outer loop and keys the inner one, so a value sitting
    where we most expect it comes before an unrelated same-named key deeper
    in the document. The query string is last - it is the fallback we
    control, not what SignalWire is expected to send.
    """
    found: list[str] = []
    for path in _CONTAINER_PATHS:
        container = _dig(payload, path)
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                found.append(str(value).strip())
    for key in keys:
        value = query.get(key)
        if value and str(value).strip():
            found.append(str(value).strip())
    return found


def _extract(payload: dict, query: dict, keys: tuple[str, ...]) -> str:
    """First non-empty value for any of ``keys``. "" when there is none."""
    candidates = _candidates(payload, query, keys)
    return candidates[0] if candidates else ""


def _extract_lead_number(payload: dict, query: dict) -> str | None:
    """First candidate that is actually DIALABLE, not merely first present.

    This is the one place where "first non-empty wins" is not good enough.
    The payload shape is a guess, so a wrong guess should not be fatal: if
    the highest-priority location happens to hold something that isn't a
    phone number, falling through to the next candidate turns a dead call
    into a connected one. Every rejected candidate is logged, so the first
    live call still tells us which location was the right one.
    """
    for candidate in _candidates(payload, query, _LEAD_KEYS):
        normalized = normalize_lead_number(candidate)
        if normalized:
            return normalized
        logger.warning(
            f"sw-dialer-connect skipping lead candidate {candidate!r} - not a "
            "dialable E.164 number"
        )
    return None


def normalize_lead_number(raw: str) -> str | None:
    """E.164 or None. None means "do not dial this"."""
    if not raw:
        return None
    cleaned = _PHONE_NOISE.sub("", str(raw).strip())
    if not cleaned:
        return None
    if not cleaned.startswith("+"):
        if not cleaned.isdigit():
            return None
        # NANP shorthand, the only two forms a US rep actually types.
        # 11 digits starting with 1 is already country-code-prefixed, so it
        # takes a bare "+"; a bare 10-digit number is missing the 1.
        if len(cleaned) == 11 and cleaned.startswith("1"):
            cleaned = f"+{cleaned}"
        elif len(cleaned) == 10:
            cleaned = f"+1{cleaned}"
        else:
            # Anything else is assumed already to carry its own country code
            # (e.g. a pasted "447700900123"). The regex below is the judge.
            cleaned = f"+{cleaned}"
    return cleaned if _E164.match(cleaned) else None


def _redacted_query(request: Request) -> dict:
    return {
        k: ("<redacted>" if k == "k" else v) for k, v in request.query_params.items()
    }


async def _read_payload(request: Request) -> dict:
    """Body as a dict, whatever SignalWire actually sends.

    The body can only be read once, so it is read raw and then parsed as
    JSON, falling back to form encoding. An unparseable body yields {} - the
    caller still has the query string, and still logs what arrived.
    """
    try:
        raw = await request.body()
    except Exception as exc:  # noqa: BLE001 - a webhook must never 500 on a read
        logger.error(f"SW webhook body read failed: {exc}")
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        # A JSON array/scalar is not usable as a container, but wrapping it
        # keeps it visible in the payload log rather than silently dropped.
        return {"_non_object_body": parsed}
    except Exception:  # noqa: BLE001 - not JSON, try form encoding
        pass
    try:
        return dict(parse_qsl(raw.decode("utf-8", errors="replace")))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"SW webhook body was neither JSON nor form-encoded: {exc}")
        return {}


def _log_payload(endpoint: str, request: Request, payload: dict) -> None:
    try:
        body = json.dumps(payload, indent=2, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001 - logging must never break the handler
        body = repr(payload)
    logger.warning(
        f"SW-DIALER-PAYLOAD: endpoint={endpoint} "
        f"query={_redacted_query(request)} body={body}"
    )


def _secret_ok(endpoint: str, request: Request) -> bool:
    """Shared-secret check via ``?k=``.

    SignalWire's request-signing scheme for SWML webhooks is unverified on
    this account, so a signature check would be a guess that silently fails
    closed on every real call. A query-string secret is verifiable and
    proportionate here: /sw-dialer-connect only RETURNS a SWML document, it
    does not place a call. The blast radius of a forged request is therefore
    a spurious dialer_calls row, not an outbound call at our expense.

    An unset SIGNALWIRE_WEBHOOK_KEY allows the request - the alternative is a
    dialer that cannot connect at all until someone notices an env var - but
    says so loudly on every single request.
    """
    expected = (os.environ.get("SIGNALWIRE_WEBHOOK_KEY") or "").strip()
    if not expected:
        logger.warning(
            f"SIGNALWIRE_WEBHOOK_KEY is unset - {endpoint} is UNAUTHENTICATED. "
            "Set it in the Dograh .env and append ?k=<key> to the SWML endpoint URL."
        )
        return True
    return hmac.compare_digest(str(request.query_params.get("k", "")), expected)


async def _is_signalwire_owned_number(number: str) -> bool:
    """True only if dialer_phone_numbers says SignalWire owns this number.

    Fails CLOSED (returns False) on any error, unlike most lookups on this
    path - see _resolve_signalwire_caller_id for why "don't know" has to mean
    "don't use it".
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_phone_numbers",
                params={
                    "select": "phone_number",
                    "phone_number": f"eq.{number}",
                    "provider": "eq.signalwire",
                    "limit": "1",
                },
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - never break call setup
        logger.error(f"SignalWire ownership check failed for {number}: {exc}")
        return False
    return isinstance(rows, list) and len(rows) > 0


async def _resolve_signalwire_caller_id(identity: str) -> str:
    """The number to dial FROM - and it must be one SignalWire owns.

    resolve_assigned_caller_id is provider-agnostic: it returns whatever
    number dialer_phone_numbers has assigned to this rep, which today is
    overwhelmingly a TWILIO number. Handing a Twilio-owned number to
    SignalWire as the caller ID gets the call rejected outright, so a
    per-rep assignment is only honoured when we can show it is SignalWire's:
    either it IS the configured SignalWire default, or dialer_phone_numbers
    marks it provider='signalwire'. Everything else falls back to the env
    default.

    Kept deliberately simple - two positive checks and a fallback - because
    the failure mode of getting this wrong is every dialer call failing, and
    the fallback is always a number we know works.
    """
    env_default = (os.environ.get("SIGNALWIRE_DEFAULT_CALLER_ID") or "").strip()

    assigned = ""
    try:
        assigned = (await resolve_assigned_caller_id(identity) or "").strip()
    except Exception as exc:  # noqa: BLE001 - contract is never-raise, but belt and braces
        logger.error(f"Assigned caller-id lookup failed for {identity!r}: {exc}")

    if assigned:
        if assigned == env_default:
            return assigned
        if await _is_signalwire_owned_number(assigned):
            return assigned
        logger.warning(
            f"Rep {identity!r} is assigned {assigned}, which is not known to be a "
            f"SignalWire number - falling back to SIGNALWIRE_DEFAULT_CALLER_ID"
        )
    return env_default


async def _rep_supabase_id(identity: str) -> str | None:
    """Supabase user id behind a 'rep-{n}' identity, or None."""
    rep_id = _parse_rep_id_from_identity(identity)
    if rep_id is None:
        return None
    try:
        user = await db_client.get_user_by_id(rep_id)
    except Exception as exc:  # noqa: BLE001 - a DB blip must not break the call
        logger.error(f"Rep lookup failed for {identity!r}: {exc}")
        return None
    return user.provider_id if user and user.provider_id else None


def _swml(document: dict) -> JSONResponse:
    return JSONResponse(content=document)


def _webhook_url(backend_endpoint: str, path: str, call_id: str) -> str:
    """Build a webhook URL carrying our own call id.

    The provider's payload identifies the leg it is describing -- for a connect status
    callback that is the leg being DIALLED, not the one we correlate on -- so our id has
    to travel in the query string or the callback cannot be matched to a call.
    """
    backend_endpoint = str(backend_endpoint or "")
    if not backend_endpoint.startswith(("http://", "https://")):
        return ""
    params = {"call_id": call_id}
    secret = (os.environ.get("SIGNALWIRE_WEBHOOK_KEY") or "").strip()
    if secret:
        params["k"] = secret
    return f"{backend_endpoint}/api/v1/telephony/{path}?" + urlencode(params)


def _tap_websocket_url(backend_endpoint: str, call_id: str) -> str:
    """The wss:// address SignalWire should fork this call's audio to.

    Same shape and same shared secret as the HTTP webhooks, over ws. Built from the
    backend endpoint rather than configured separately so it can never point somewhere
    the webhooks do not -- a tap aimed at the wrong host is a live call's audio leaving
    for a host we do not run.

    Returns "" when there is no usable endpoint, and the SWML then omits the tap
    entirely. Monitoring is worth having; it is not worth a malformed SWML document on
    a live call.
    """
    # Kill switch. The tap rides on every live call, and SignalWire's handling of the
    # verb is not yet proven on a real one -- join_room was documented, correctly named,
    # and threw a 500 that tore the leg down after 101 seconds. If tapping ever breaks
    # calls, this turns it off in the time it takes to edit .env and restart, without
    # waiting for anybody to revert code.
    #
    # Defaults ON, because a feature nobody has switched on is not a feature. Anything
    # falsey and explicit turns it off.
    if (os.environ.get("SYSEVO_DIALER_TAP") or "").strip().lower() in ("0", "off", "false", "no"):
        return ""

    http_url = _webhook_url(backend_endpoint, "sw-tap", call_id)
    if not http_url:
        return ""
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):]
    return "ws://" + http_url[len("http://"):]


@router.post("/sw-dialer-connect", include_in_schema=False)
async def handle_sw_dialer_connect(request: Request):
    """Return SWML bridging the rep to the lead.

    Wrapped end-to-end: ANY exception becomes hangup SWML. A 500 here is a
    rep listening to silence with nothing in the UI to explain it.
    """
    try:
        payload = await _read_payload(request)
        _log_payload("sw-dialer-connect", request, payload)

        if not _secret_ok("sw-dialer-connect", request):
            logger.warning("sw-dialer-connect rejected: bad or missing ?k= secret")
            return _swml(build_hangup_swml())

        query = dict(request.query_params)
        entry_id = _extract(payload, query, _ENTRY_KEYS) or None
        identity = _extract(payload, query, _REP_KEYS)

        # Accepting an inbound call: the caller is already held in a named conference and
        # this leg joins them, rather than dialling anyone.
        #
        # Checked before the lead lookup because an accepting rep has no lead number to
        # find - falling through would hit the "no dialable lead number" hangup and drop
        # a caller who is already on the line. The conference name comes from the browser,
        # but it is not a capability: it is only ever handed out by inbound_claim, which
        # refuses a call that was not ringing for that rep.
        conference_name = _extract(payload, query, _CONFERENCE_KEYS)
        if conference_name:
            logger.info(
                f"sw-dialer-connect joining conference {conference_name!r} for {identity!r}"
            )
            return _swml(build_conference_join_swml(conference_name=conference_name))

        lead_number = _extract_lead_number(payload, query)
        if not lead_number:
            logger.error(
                "sw-dialer-connect found no dialable lead number anywhere in the "
                "payload or query string - hanging up"
            )
            return _swml(build_hangup_swml())

        caller_id = await _resolve_signalwire_caller_id(identity)
        if not caller_id:
            logger.error(
                "sw-dialer-connect has no usable caller ID - set "
                "SIGNALWIRE_DEFAULT_CALLER_ID - hanging up"
            )
            return _swml(build_hangup_swml())

        # Correlation key for the status/recording callbacks. Preferred from
        # the payload so those callbacks can find this row; a synthetic id
        # keeps the row (and the rep's call history) rather than dropping it
        # when the payload carries no id we recognise.
        call_id = _extract(payload, query, _CALL_ID_KEYS) or f"sw-{uuid.uuid4().hex}"

        rep_user_id = await _rep_supabase_id(identity)
        if rep_user_id:
            await create_dialer_call(
                parent_call_sid=call_id,
                rep_user_id=rep_user_id,
                entry_id=entry_id,
                from_number=caller_id,
                to_number=lead_number,
                provider="signalwire",
            )
        else:
            logger.warning(
                f"sw-dialer-connect could not map identity {identity!r} to a Supabase "
                "user - connecting the call anyway, but it will not appear in Recent Calls"
            )

        # An unresolvable backend endpoint omits recording rather than
        # failing: build_dialer_swml drops record_call for an empty webhook,
        # and a call with no recording beats a call that does not connect.
        recording_webhook = ""
        try:
            backend_endpoint, _ = await get_backend_endpoints()
        except Exception as exc:  # noqa: BLE001 - never break call setup
            logger.error(f"sw-dialer-connect could not resolve backend endpoint: {exc}")
            backend_endpoint = ""
        backend_endpoint = str(backend_endpoint or "")
        if backend_endpoint.startswith(("http://", "https://")):
            # urlencode, not concatenation: call_id comes out of a payload we
            # do not control, so an "&" or a space in it would otherwise
            # rewrite the query string and lose the secret.
            recording_webhook = _webhook_url(backend_endpoint, "sw-recording", call_id)

        # The far end's own progress. Nothing else reports it: the resource-level Status
        # Change Webhook describes this script's leg, not the leg being dialled, which is
        # why sw-call-status has never fired despite being configured for months.
        call_state_webhook = _webhook_url(backend_endpoint, "sw-call-status", call_id)

        return _swml(
            build_dialer_swml(
                lead_number=lead_number,
                caller_id=caller_id,
                call_state_webhook=call_state_webhook,
                recording_webhook=recording_webhook,
                tap_websocket=_tap_websocket_url(backend_endpoint, call_id),
            )
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: a live leg is waiting
        logger.exception(f"sw-dialer-connect failed, hanging up: {exc}")
        return _swml(build_hangup_swml())


def _map_call_state(state: str, end_reason: str) -> str:
    """SignalWire's call_state vocabulary onto dialer_calls.status.

    dialer_calls.status has a CHECK constraint, so an unmapped value does not merely look
    odd -- it fails the write and the call keeps whatever status it already had, silently.
    "answered" and "ended" are SWML's words; ours are "in-progress" and "completed".

    An end_reason turns a bare "ended" into something a rep can act on: no-answer and
    busy are worth another attempt, declined is not.
    """
    normalised = (state or "").strip().lower()
    reason = (end_reason or "").strip().lower()

    if normalised == "answered":
        return "in-progress"
    if normalised == "ringing":
        return "ringing"
    if normalised == "created":
        return "initiated"
    if normalised == "ended":
        return {
            "no_answer": "no-answer",
            "busy": "busy",
            "declined": "busy",
            "cancel": "canceled",
            "error": "failed",
        }.get(reason, "completed")
    # Already one of ours (Twilio's callback speaks this vocabulary directly).
    return normalised or "completed"


@router.post("/sw-call-status", include_in_schema=False)
async def handle_sw_call_status(request: Request):
    """Status callback. No live leg waits on this, so a bad secret is a 401
    rather than SWML."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-call-status", request, payload)

        if not _secret_ok("sw-call-status", request):
            logger.warning("sw-call-status rejected: bad or missing ?k= secret")
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

        query = dict(request.query_params)
        # OUR call id, from the query string, in preference to anything in the body.
        #
        # A connect status callback describes the leg being DIALLED, so params.call_id is
        # the far end's id and would never match a dialer_calls row. The id we correlate
        # on is the one we put in the URL when building the webhook.
        call_id = (query.get("call_id") or "").strip() or _extract(
            payload, query, _CALL_ID_KEYS
        )
        state = _extract(payload, query, _STATE_KEYS)
        raw_duration = _extract(payload, query, _DURATION_KEYS)
        # Why it ended, when the provider says. Turns a bare "ended" into no-answer,
        # busy or declined -- the difference between a call worth retrying and one that
        # was actively refused.
        end_reason = _extract(payload, query, _END_REASON_KEYS)

        if not call_id or not state:
            logger.warning(
                f"sw-call-status had no usable call id ({call_id!r}) or state "
                f"({state!r}) - nothing to update"
            )
            return JSONResponse(content={"ok": True})

        duration = None
        if raw_duration:
            try:
                duration = int(float(raw_duration))
            except (TypeError, ValueError):
                logger.warning(f"sw-call-status duration {raw_duration!r} is not a number")

        await update_dialer_call_status(
            parent_call_sid=call_id,
            child_call_sid=None,
            status=_map_call_state(state, end_reason),
            duration_seconds=duration,
        )
        return JSONResponse(content={"ok": True})
    except Exception as exc:  # noqa: BLE001 - a status callback must never 500
        logger.exception(f"sw-call-status failed: {exc}")
        return JSONResponse(content={"ok": True})


@router.post("/sw-recording", include_in_schema=False)
async def handle_sw_recording(request: Request):
    """Recording callback. Same 401-not-SWML reasoning as sw-call-status."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-recording", request, payload)

        if not _secret_ok("sw-recording", request):
            logger.warning("sw-recording rejected: bad or missing ?k= secret")
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

        query = dict(request.query_params)
        call_id = _extract(payload, query, _CALL_ID_KEYS)
        recording_url = _extract(payload, query, _RECORDING_URL_KEYS)

        # Nested {"recording": {"url": ...}} is common enough in callback
        # payloads to be worth one explicit extra probe, since "recording"
        # is not one of the generic containers.
        if not recording_url:
            nested = _dig(payload, ("recording",))
            if isinstance(nested, dict):
                recording_url = _extract(nested, {}, _RECORDING_URL_KEYS)

        if not call_id or not recording_url:
            logger.warning(
                f"sw-recording had no usable call id ({call_id!r}) or recording url "
                f"({recording_url!r}) - nothing to update"
            )
            return JSONResponse(content={"ok": True})

        await update_dialer_call_recording_url(
            parent_call_sid=call_id, recording_url=recording_url
        )
        return JSONResponse(content={"ok": True})
    except Exception as exc:  # noqa: BLE001 - a recording callback must never 500
        logger.exception(f"sw-recording failed: {exc}")
        return JSONResponse(content={"ok": True})


# ── Inbound ──────────────────────────────────────────────────────────────────────


_FROM_KEYS = ("from", "From", "from_number", "caller_id_number", "callerIdNumber")
_TO_KEYS = ("to", "To", "to_number", "called_number", "calledNumber")


def _clean_number(raw: str) -> str:
    """A SIP URI or a bare number down to something dialable and comparable.

    Inbound `from` arrives in several shapes depending on how the call reached the space
    ("+447700900000", "sip:+447700900000@...", "17705551234"). The stored number and the
    one we compare against dialer_phone_numbers must be the same shape or assignment
    routing silently never matches.
    """
    value = (raw or "").strip()
    if value.startswith("sip:"):
        value = value[4:]
    value = value.split("@", 1)[0]
    value = value.split(";", 1)[0]
    digits = re.sub(r"[^\d+]", "", value)
    if digits and not digits.startswith("+"):
        digits = "+" + digits.lstrip("+")
    return digits


@router.post("/sw-inbound", include_in_schema=False)
async def handle_sw_inbound(request: Request):
    """Answer an inbound call, hold the caller, and ring whoever can take it.

    Wrapped end-to-end for the same reason as sw-dialer-connect: any exception here is a
    real person listening to silence, so every failure path still returns playable SWML.
    """
    try:
        payload = await _read_payload(request)
        _log_payload("sw-inbound", request, payload)

        if not _secret_ok("sw-inbound", request):
            logger.warning("sw-inbound rejected: bad or missing ?k= secret")
            return _swml(build_hangup_swml())

        query = dict(request.query_params)
        from_number = _clean_number(_extract(payload, query, _FROM_KEYS))
        to_number = _clean_number(_extract(payload, query, _TO_KEYS))
        call_id = _extract(payload, query, _CALL_ID_KEYS) or f"in-{uuid.uuid4().hex}"

        # Neither end identified means this is not a call we can route or attribute:
        # a probe, a provider health check, or a payload shape we cannot read. Creating a
        # row for it rings the whole team for nobody, and if somebody answers, that row
        # has no status callback to close it -- which locked a real rep out of the ring
        # pool for an hour. Refused before anything is written.
        if not from_number and not to_number:
            logger.warning(
                "sw-inbound: payload identified neither caller nor destination - refusing"
            )
            return _swml(build_hangup_swml())

        if not from_number:
            # Withheld or malformed caller id. The call is still worth taking - a rep can
            # answer "unknown caller" - so this is recorded rather than refused.
            logger.warning("sw-inbound could not read a caller number from the payload")
            from_number = "unknown"

        # One conference per call, named from the provider's own call id so the name is
        # unique and reconstructible from either end.
        conference_name = f"inbound-{call_id}"

        plan = await resolve_inbound_plan(to_number)
        targets = [str(p["user_id"]) for p in plan if p.get("user_id")]

        await create_inbound_call(
            provider_call_id=call_id,
            from_number=from_number,
            to_number=to_number,
            conference_name=conference_name,
            target_user_ids=targets,
        )

        if not targets:
            # Nobody to ring. The row is already logged as missed, so the team still
            # sees the call - but the caller must be told, not parked in a conference
            # that will never be joined.
            logger.warning(
                f"sw-inbound: nobody available for {to_number} - caller {from_number} "
                "told and hung up"
            )
            return _swml(build_no_agents_swml())

        recording_webhook = ""
        try:
            backend_endpoint, _ = await get_backend_endpoints()
        except Exception as exc:  # noqa: BLE001 - never break call setup
            logger.error(f"sw-inbound could not resolve backend endpoint: {exc}")
            backend_endpoint = ""
        backend_endpoint = str(backend_endpoint or "")
        if backend_endpoint.startswith(("http://", "https://")):
            params = {"call_id": call_id}
            secret = (os.environ.get("SIGNALWIRE_WEBHOOK_KEY") or "").strip()
            if secret:
                params["k"] = secret
            recording_webhook = (
                f"{backend_endpoint}/api/v1/telephony/sw-recording?" + urlencode(params)
            )

        # Hold the caller on the Sysevo line and bring the person to it.
        #
        # No forwarding to personal numbers, deliberately. Forwarding looked like the way
        # to reach somebody away from their desk, and in practice it routed every call to
        # a mobile that could not be reached and turned "nobody is at their screen" into
        # "rings once, then cuts off" -- a dead end that looked like a broken dialer.
        #
        # A colleague's line now behaves like a desk phone: it keeps ringing. Whoever is
        # being rung gets a push the moment the row exists (fired off the critical path,
        # so it never delays this response), and answering brings them into the conference
        # the caller is already waiting in. Live or push, the treatment is the same --
        # the difference is only how quickly they notice.
        if plan:
            return _swml(
                build_inbound_hold_swml(
                    conference_name=conference_name,
                    recording_webhook=recording_webhook,
                    # SPOKEN_COMPANY_NAME, not "Sysevo": this string is read
                    # aloud by TTS, which says "Sys-AY-vo" for the real spelling.
                    greeting=(
                        f"Thanks for calling {SPOKEN_COMPANY_NAME}. "
                        "Please hold while we connect you to the team."
                    ),
                    # The caller's leg carries both sides of the conversation once a rep
                    # joins, so tapping it is enough to monitor the whole call.
                    tap_websocket=_tap_websocket_url(backend_endpoint, call_id),
                )
            )

        # Nobody to ring at all: nobody on shift, everybody on do-not-disturb, or already
        # on a call. The row is logged so the team sees it; the caller is told rather than
        # held for somebody who is never coming.
        logger.warning(f"sw-inbound: nobody reachable for {to_number}")
        return _swml(build_no_agents_swml())
    except Exception as exc:  # noqa: BLE001 - a live caller is waiting
        logger.exception(f"sw-inbound failed, hanging up: {exc}")
        return _swml(build_hangup_swml())


@router.post("/sw-inbound-status", include_in_schema=False)
async def handle_sw_inbound_status(request: Request):
    """Close out an inbound call when the caller hangs up.

    Without this an unanswered call stays 'ringing' forever: every rep's browser keeps
    ringing for somebody who has already gone, and the answering rep stays marked busy
    because availability treats an open answered call as in-progress.
    """
    try:
        payload = await _read_payload(request)
        _log_payload("sw-inbound-status", request, payload)

        if not _secret_ok("sw-inbound-status", request):
            return {"status": "rejected"}

        query = dict(request.query_params)
        call_id = _extract(payload, query, _CALL_ID_KEYS)
        state = (_extract(payload, query, _STATE_KEYS) or "").lower()
        if not call_id:
            return {"status": "ignored", "reason": "no_call_id"}

        if state in ("ended", "completed", "hangup", "canceled", "cancelled", "failed"):
            # Only a call still ringing becomes 'missed'. One that was answered keeps that
            # status and just gains an end time - overwriting it would erase the fact that
            # somebody picked up.
            await close_inbound_call(provider_call_id=call_id)
        return {"status": "success"}
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"sw-inbound-status failed: {exc}")
        return {"status": "error"}


# ── Live audio: taps and listeners ──────────────────────────────────────────────
#
# SWML's `tap` verb forks a live call's audio to a WebSocket while the call carries on.
# One tap feeds everything that needs to hear a call in progress: a manager monitoring,
# and later live transcription. See tap_relay for why the fan-out goes through Redis.


async def _supabase_user_id(access_token: str) -> str | None:
    """The Supabase user behind an access token, or None if it is not a valid one."""
    if not access_token or not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
                timeout=5.0,
            )
        if response.status_code != 200:
            return None
        user_id = response.json().get("id")
        return str(user_id) if user_id else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"listen auth: could not verify token: {exc}")
        return None


async def _may_monitor_calls(user_id: str) -> bool:
    """Only admins and sales managers may listen to a live call.

    Fails CLOSED, unlike most lookups on this path. Everywhere else in this module a
    Supabase blip must not cost somebody their call; here it must not hand somebody a
    live customer conversation. "We could not check" is not permission.
    """
    if not user_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/user_roles",
                params={
                    "select": "role",
                    "user_id": f"eq.{user_id}",
                    "role": "in.(super_admin,sales_manager)",
                    "limit": "1",
                },
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            return bool(response.json())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"listen auth: role check failed for {user_id}: {exc}")
        return False


@router.websocket("/sw-tap")
async def websocket_sw_tap(websocket: WebSocket):
    """SignalWire's live audio for one call, published for listeners.

    Authenticated by the same ?k= shared secret as the HTTP webhooks, checked BEFORE the
    handshake is accepted -- an accepted socket that is then closed still tells an
    unauthenticated caller that the endpoint exists and that a call id was valid.
    """
    call_id = (websocket.query_params.get("call_id") or "").strip()
    expected = (os.environ.get("SIGNALWIRE_WEBHOOK_KEY") or "").strip()
    if expected and not hmac.compare_digest(
        str(websocket.query_params.get("k", "")), expected
    ):
        await websocket.close(code=4401, reason="bad key")
        return
    if not call_id:
        await websocket.close(code=4400, reason="missing call_id")
        return
    if not expected:
        logger.warning("SIGNALWIRE_WEBHOOK_KEY is unset - /sw-tap is UNAUTHENTICATED.")

    await websocket.accept()
    logger.info(f"tap opened for call {call_id}")

    async def frames():
        """Binary messages only.

        SignalWire opens with a JSON handshake on some transports and sends audio as
        binary after it. Text is skipped rather than published: relaying it would put
        JSON into a listener's audio stream as a burst of noise.
        """
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data:
                yield data

    try:
        # Transcription rides on the same frames rather than opening a second tap: one
        # fork of the audio, two consumers. It disables itself when DEEPGRAM_API_KEY is
        # unset, so listening works with or without captions.
        async def on_caption(payload: dict) -> None:
            await publish_text(call_id, payload)

        async with LiveTranscriber(call_id, on_caption) as transcriber:
            relayed = await publish_frames(
                call_id, frames(), sink=transcriber.feed if transcriber.enabled else None
            )
        logger.info(f"tap for call {call_id} closed after {relayed} frames")
    except WebSocketDisconnect:
        logger.info(f"tap for call {call_id} disconnected")
    except Exception as exc:  # noqa: BLE001 - a monitoring fault must never surface
        logger.error(f"tap for call {call_id} failed: {exc}")


def _looks_like_uuid(value: str) -> bool:
    """Whether a string can be used in an ``id=eq.`` filter on a uuid column.

    PostgREST answers 400 to a non-uuid there, and a 400 on this path reads as "no such
    call" -- so an unchecked value would turn a provider call id into a denial rather
    than a lookup that simply misses.
    """
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


async def _supabase_rows(table: str, params: dict) -> list[dict]:
    """One service-role read. Returns [] on any failure.

    Empty means "no", never "we could not tell": every caller below fails CLOSED, for
    the same reason as _may_monitor_calls -- a Supabase blip must not hand somebody a
    live customer conversation.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params=params,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"listen auth: read of {table} failed: {exc}")
        return []


async def _resolve_live_call(call_id: str, user_id: str) -> tuple[str | None, bool]:
    """The tap's channel key for a call, and whether this user is on that call.

    ONE lookup answers both, deliberately, because as two they disagreed and the
    disagreement was invisible. Authorisation matched a row by ``dialer_calls.id`` and
    the subscription then used that same UUID as the Redis channel -- but /sw-tap
    publishes under the PROVIDER's call id (``parent_call_sid``), the only id SWML has
    at the moment the tap is attached. So every listener authenticated, opened a
    socket, and subscribed to a channel nothing had ever published to. A healthy,
    silent socket: exactly how the coaching panel spent months saying "Listening" and
    how live listen-in produced no audio.

    Returns (channel key, is-own-call). No key means there is no such live call.
    """
    if not call_id:
        return None, False

    # Not a UUID: already a provider call id, i.e. the tap's own key. It identifies no
    # row and no rep, so no own-call claim can rest on it -- returned unowned, and only
    # a monitoring role gets through on it.
    if not _looks_like_uuid(call_id):
        return call_id, False

    outbound = await _supabase_rows(
        "dialer_calls",
        {"select": "parent_call_sid,rep_user_id", "id": f"eq.{call_id}", "limit": "1"},
    )
    if outbound:
        row = outbound[0]
        key = str(row.get("parent_call_sid") or "").strip()
        return (key or None), bool(user_id) and str(row.get("rep_user_id") or "") == user_id

    # An answered inbound call has NO dialer_calls row: inbound is logged in
    # inbound_calls, and the tap rides the caller's held leg keyed on provider_call_id.
    inbound = await _supabase_rows(
        "inbound_calls",
        {
            "select": "provider_call_id,answered_by,target_user_ids",
            "id": f"eq.{call_id}",
            "limit": "1",
        },
    )
    if inbound:
        row = inbound[0]
        key = str(row.get("provider_call_id") or "").strip()
        targets = [str(t) for t in (row.get("target_user_ids") or [])]
        # Rung-for counts as well as answered-by. answered_by is stamped by a callback
        # that may not have landed yet, and the rep is already mid-sentence.
        own = bool(user_id) and (
            str(row.get("answered_by") or "") == user_id or user_id in targets
        )
        return (key or None), own

    return None, False


@router.websocket("/sw-listen")
async def websocket_sw_listen(websocket: WebSocket):
    """A manager listening to a live call.

    Receive-only by design. This socket never carries audio back, so monitoring cannot
    become barge-in by accident -- neither the rep nor the person they are speaking to can
    be interrupted by somebody who only meant to listen.
    """
    call_id = (websocket.query_params.get("call_id") or "").strip()
    token = (websocket.query_params.get("token") or "").strip()
    # A rep watching their own captions has no use for audio of a call they are on, and
    # sending it would put their own voice back in their ear a fifth of a second late.
    captions_only = websocket.query_params.get("captions_only") in ("1", "true", "yes")

    if not call_id:
        await websocket.close(code=4400, reason="missing call_id")
        return

    user_id = await _supabase_user_id(token)
    # The channel to subscribe to and the right to subscribe to it come from the same
    # lookup, so they cannot drift apart again. See _resolve_live_call.
    channel_key, own_call = (
        await _resolve_live_call(call_id, user_id) if user_id else (None, False)
    )
    permitted = (
        bool(user_id)
        and bool(channel_key)
        and (
            await _may_monitor_calls(user_id)
            or (captions_only and own_call)
        )
    )
    if not permitted:
        # Closed before accept, and with the same code whether the token was bad, the
        # role was wrong or the call does not exist: telling those apart is a way to
        # probe who is a manager and which call ids are real.
        await websocket.close(code=4403, reason="not permitted")
        return

    await websocket.accept()
    logger.info(
        f"listener {user_id} attached to call {call_id} on tap channel {channel_key}"
    )

    stop = asyncio.Event()

    async def watch_for_close():
        """A listener who closes the tab must stop the subscription.

        Without this the Redis subscribe loop would keep running for the life of the
        call, one leaked task per manager who ever looked.
        """
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            stop.set()

    watcher = asyncio.create_task(watch_for_close())
    frames_sent = 0
    try:
        async for kind, payload in subscribe_stream(channel_key, stop):
            # Audio as binary, captions as text. The browser tells them apart by the
            # frame type rather than by inspecting bytes, which is why they travel on
            # separate Redis channels in the first place.
            if kind == "text":
                await websocket.send_text(payload.decode("utf-8", "replace"))
                continue
            if captions_only:
                continue
            await websocket.send_bytes(payload)
            frames_sent += 1
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"listener {user_id} on call {call_id} ended: {exc}")
    finally:
        stop.set()
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        logger.info(f"listener {user_id} left call {call_id} after {frames_sent} frames")
