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

import hmac
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import APIRouter, Request
from loguru import logger
from starlette.responses import JSONResponse

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer.swml import build_dialer_swml, build_hangup_swml
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
            params = {"call_id": call_id}
            secret = (os.environ.get("SIGNALWIRE_WEBHOOK_KEY") or "").strip()
            if secret:
                params["k"] = secret
            recording_webhook = (
                f"{backend_endpoint}/api/v1/telephony/sw-recording?"
                + urlencode(params)
            )

        return _swml(
            build_dialer_swml(
                lead_number=lead_number,
                caller_id=caller_id,
                recording_webhook=recording_webhook,
            )
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: a live leg is waiting
        logger.exception(f"sw-dialer-connect failed, hanging up: {exc}")
        return _swml(build_hangup_swml())


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
        call_id = _extract(payload, query, _CALL_ID_KEYS)
        state = _extract(payload, query, _STATE_KEYS)
        raw_duration = _extract(payload, query, _DURATION_KEYS)

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
            status=state,
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
