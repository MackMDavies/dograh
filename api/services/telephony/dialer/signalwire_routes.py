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
import hashlib
import hmac
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
import re
import time
import uuid
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer.swml import (
    build_conference_join_swml,
    SPOKEN_COMPANY_NAME,
    build_dialer_swml,
    build_hangup_swml,
    build_inbound_hold_swml,
    build_agent_overflow_swml,
    build_no_agents_swml,
    build_signalwire_parallel_leg_swml,
    build_signalwire_parallel_overflow_swml,
    build_signalwire_parallel_rep_swml,
    build_signalwire_parallel_voicemail_swml,
)
from api.services.telephony.dialer import signalwire_parallel_store
from api.services.telephony.dialer.compliance import (
    calling_hours_allowed,
    calling_hours_result,
    phone_block_reason,
)
from api.services.telephony.dialer.live_transcribe import LiveTranscriber
from api.services.telephony.dialer.tap_relay import (
    publish_frames,
    publish_text,
    subscribe_stream,
)
from api.services.telephony.dialer.inbound_call_log import (
    bind_inbound_rep_leg,
    close_inbound_call,
    close_overflow_conference,
    create_inbound_call,
    expire_unanswered_inbound_call,
    is_claimed_inbound_conference,
    notify_inbound_call,
    record_overflow_join,
    evaluate_overflow_guard,
    resolve_inbound_plan,
)
from api.services.telephony.providers.twilio.dialer_call_log import (
    create_dialer_call,
    effective_auto_send,
    is_provider_playable_voicemail,
    resolve_effective_voicemail_drop,
    update_dialer_call_recording_url,
    update_dialer_call_status,
    update_signalwire_dialer_amd,
)
from api.services.telephony.providers.twilio.dialer_number_assignment import (
    DialerNumberAssignmentUnavailable,
    DialerNumberProviderMismatch,
    _parse_rep_id_from_identity,
    resolve_assigned_caller_id,
)
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.dialer import sam_handoff
from api.services.telephony.dialer import sam_knowledge
from api.services.telephony.dialer.sam_knowledge_rules import answer_for
from api.services.telephony.dialer.sam_handoff_rules import (
    REP_ANSWER_SECONDS,
    knowledge_request_allowed,
    transfer_request_allowed,
)
from api.services.telephony.dialer.inbound_agent import current_inbound_agent, number_hands_to_agent
from api.utils.common import get_backend_endpoints

router = APIRouter()

_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """Keep post-response call cleanup and notifications alive until completion."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _expire_signalwire_overflow_offer(provider_call_id: str) -> None:
    """End a second caller only if their teammate invitation was never claimed."""
    await asyncio.sleep(20)
    if not await expire_unanswered_inbound_call(
        provider_call_id=provider_call_id, provider="signalwire"
    ):
        return
    try:
        from api.services.telephony.dialer.signalwire_calling import end_call
        await end_call(call_id=provider_call_id, reason="hangup")
    except Exception as exc:  # noqa: BLE001 - an expired invitation must not crash a webhook
        logger.error(f"Could not end unanswered SignalWire overflow caller {provider_call_id}: {exc}")
    await evaluate_overflow_guard(provider_call_id=provider_call_id, provider="signalwire")

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
_END_SOURCE_KEYS = ("end_source", "endSource")


def _ended_by(state: str, end_source: str, end_reason: str) -> str | None:
    """Which side hung up, only when SignalWire says it was a normal hangup.

    The connect's call_state events describe the leg being DIALLED, and on its
    ``ended`` event ``end_source`` says where the hangup came from. Calibrated on
    2026-09-25 against calls whose ending is known: ``outbound`` on every call a rep
    ended (including voicemails they left), ``inbound`` on every call the prospect
    ended with a goodbye. Three calls reps reported as "cut off mid-sentence" were all
    ``inbound`` with ``end_reason: hangup``: the prospect's side of the line.

    Until this was stored, nothing could say so. SignalWire's record of a cut rep and
    a callee who hung up look alike everywhere else, and the rep's wrap-up read "Call
    ended" either way.
    """
    if (state or "").strip().lower() != "ended":
        return None
    # end_source describes the leg SignalWire was trying to connect, not
    # necessarily a person who actually hung up. In live US logs, `noAnswer`
    # arrives with end_source=`outbound`; treating that as a rep hangup makes a
    # ring-out look like a deliberate hangup. Failed, busy, canceled and
    # unanswered calls have no human side to attribute.
    if re.sub(r"[^a-z]", "", (end_reason or "").strip().lower()) != "hangup":
        return None
    source = (end_source or "").strip().lower()
    if source == "outbound":
        return "rep"
    if source == "inbound":
        return "prospect"
    return None
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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
        k: ("<redacted>" if k in {"k", "sig"} else v)
        for k, v in request.query_params.items()
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


def _secret_ok(
    endpoint: str,
    request: Request,
    *,
    allow_per_call_signature: bool = False,
) -> bool:
    """Authenticate provider webhooks before returning call instructions.

    The SWML bootstrap endpoint can place an outbound PSTN call, so it must always
    require a secret in its configured SignalWire webhook URL. Other callbacks use
    the shared key or their per-call HMAC. The bootstrap secret is separate from
    callback auth so securing this static resource cannot break dynamic callbacks.
    """
    expected = ""
    if endpoint in {"sw-dialer-connect", "sw-inbound"}:
        expected = (
            os.environ.get("SIGNALWIRE_DIALER_CONNECT_KEY")
            or os.environ.get("SIGNALWIRE_WEBHOOK_KEY")
            or ""
        ).strip()
        if not expected:
            logger.error(
                f"SIGNALWIRE_DIALER_CONNECT_KEY is unset - rejecting unauthenticated "
                f"{endpoint} SWML requests"
            )
            return False
    else:
        expected = (os.environ.get("SIGNALWIRE_WEBHOOK_KEY") or "").strip()
    if not expected:
        if allow_per_call_signature:
            call_id = (request.query_params.get("call_id") or "").strip()
            supplied = str(request.query_params.get("sig", ""))
            expires_raw = str(request.query_params.get("exp", ""))
            try:
                expires_at = int(expires_raw)
            except (TypeError, ValueError):
                expires_at = 0
            derived = _webhook_signature(call_id, expires_at)
            now = int(time.time())
            if (
                call_id
                and expires_at >= now
                and expires_at <= now + 7 * 24 * 60 * 60
                and derived
                and hmac.compare_digest(supplied, derived)
            ):
                return True
            logger.warning(f"{endpoint} rejected: missing or invalid per-call signature")
            return False
        logger.warning(f"SIGNALWIRE_WEBHOOK_KEY is unset - {endpoint} is UNAUTHENTICATED.")
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

    A configured platform default is used only for reps without an assigned
    line. If a rep has rotated to another provider, a stale SignalWire client
    is rejected instead of presenting the platform default caller ID.
    """
    env_default = (os.environ.get("SIGNALWIRE_DEFAULT_CALLER_ID") or "").strip()

    assigned = ""
    try:
        assigned = (await resolve_assigned_caller_id(identity, provider_name="signalwire") or "").strip()
    except (DialerNumberProviderMismatch, DialerNumberAssignmentUnavailable) as exc:
        logger.warning(f"SignalWire dial rejected for {identity!r}: {exc}")
        return ""
    except Exception as exc:  # noqa: BLE001 - contract is never-raise, but belt and braces
        logger.error(f"Assigned caller-id lookup failed for {identity!r}: {exc}")

    if assigned:
        if assigned == env_default:
            return assigned
        if await _is_signalwire_owned_number(assigned):
            return assigned
        logger.warning(
            f"Rep {identity!r} is assigned {assigned}, which is not known to be a "
            "SignalWire number - rejecting the call rather than substituting a line."
        )
        return ""
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


async def _inbound_agent_number(number: str | None = None, *, call_id: str | None = None) -> str:
    """The number of the agent that takes callers nobody can answer, or "" for none.

    Chosen on the dialer's Agents page (see inbound_agent); until an agent is picked there,
    the old SYSEVO_SAM_INBOUND_NUMBER setting still applies. Empty keeps the old behaviour --
    a spoken "nobody available" and a hangup.
    """
    return (await current_inbound_agent(number=number,call_id=call_id)).number


async def _no_answer_swml(*, caller_number: str, rang_number: str) -> dict:
    """What a caller hears when no rep can take the call: the AI agent, or the message."""
    agent = await _inbound_agent_number(rang_number)
    if not agent:
        return build_no_agents_swml()
    if not await number_hands_to_agent(rang_number):
        logger.info(f"sw-inbound: {rang_number} is switched off for hand-over - caller {caller_number} told nobody is available")
        return build_no_agents_swml()
    logger.info(f"sw-inbound: nobody available for {rang_number} - {caller_number} handed to the inbound agent")
    # No caller_id: SignalWire presents the prospect's own number, so the agent's memory and
    # lookups see who is actually calling (see build_agent_overflow_swml).
    return build_agent_overflow_swml(agent_number=agent)


def _reroute_url(backend_endpoint: str, call_id: str, mode: str) -> str:
    """Where a live inbound call is moved to: our own SWML for that hand-off."""
    base = _webhook_url(backend_endpoint, "sw-inbound-reroute", call_id)
    return f"{base}&{urlencode({'mode': mode})}" if base else ""


async def _sam_inbound_workflow_id() -> int:
    """The inbound agent's Dograh workflow: the only agent allowed to hand a caller back or
    use the knowledge lookup. Follows the Agents page; 214 (Sam INBOUND) when nothing says."""
    return (await current_inbound_agent()).workflow_id or 214


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
    else:
        params["exp"] = str(int(time.time()) + 7 * 24 * 60 * 60)
        signature = _webhook_signature(call_id, int(params["exp"]))
        if signature:
            params["sig"] = signature
        else:
            params.pop("exp", None)
    return f"{backend_endpoint}/api/v1/telephony/{path}?" + urlencode(params)


def _webhook_signature(call_id: str, expires_at: int) -> str:
    """Sign one callback URL, with a bounded lifetime, using an existing secret."""
    secret = (
        os.environ.get("SIGNALWIRE_WEBHOOK_KEY")
        or os.environ.get("SIGNALWIRE_API_TOKEN")
        or ""
    ).strip()
    if not secret or not call_id or expires_at <= 0:
        return ""
    # v2 invalidates callback signatures already exposed by the previous
    # access-log format, while keeping callbacks scoped to a call and expiry.
    message = f"webhook-v2:{call_id}:{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _tap_token(call_id: str) -> str:
    """A credential for ONE call's tap, derived rather than configured.

    /sw-tap was unauthenticated in production for months: anyone who guessed a call id
    could open the socket and push audio frames into whatever a manager was listening to.
    The ?k= shared secret was supposed to cover it, but that secret is shared with
    the HTTP webhooks -- and those are fetched from URLs configured in the SignalWire
    DASHBOARD, which carry no k. Setting SIGNALWIRE_WEBHOOK_KEY to close this hole
    therefore made _secret_ok() reject every SWML fetch and took ALL calling down: ring,
    then immediate hangup. See the revert in project_dograh_prod_deploy.

    This cannot repeat, because the tap URL is not a dashboard URL -- WE build it, per
    call, in _tap_websocket_url below. The same function derives the token on both sides,
    from the same environment, so the two can never disagree about whether one is
    expected. There is no half-enabled state to get into.

    Per call rather than static: a token that leaks buys the holder one call id, for as
    long as that one call is live, instead of every call forever.

    Derived from a secret that is ALREADY set on every box that can place a call, so this
    needs no new environment variable and no deploy-order dance. SIGNALWIRE_WEBHOOK_KEY is
    preferred if somebody has set it; SIGNALWIRE_API_TOKEN is the fallback and is
    necessarily present -- without it there are no calls to tap. The secret itself never
    leaves the server; only the digest travels.
    """
    secret = (
        os.environ.get("SIGNALWIRE_WEBHOOK_KEY")
        or os.environ.get("SIGNALWIRE_API_TOKEN")
        or ""
    ).strip()
    if not secret or not call_id:
        return ""
    return hmac.new(
        secret.encode("utf-8"), call_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


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
    # Appended here rather than inside _webhook_url: the HTTP webhooks are fetched from
    # dashboard-configured URLs that cannot carry a per-call value, so a token on them
    # would be an expectation nothing could ever satisfy. Only the tap URL is ours.
    token = _tap_token(call_id)
    if token:
        http_url += "&" + urlencode({"t": token})
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):]
    return "ws://" + http_url[len("http://"):]


async def _start_signalwire_session(
    *, request: Request, payload: dict, query: dict, identity: str,
    screened_single: bool = False,
) -> JSONResponse:
    """Reserve and originate a SignalWire queue call through server-side AMD."""
    from api.services.telephony.dialer.parallel_auth import verify_parallel_auth_token
    token = _extract(payload, query, ("ParallelAuthToken", "parallel_auth_token"))
    token_subject = verify_parallel_auth_token(token)
    rep_subject = await _rep_supabase_id(identity)
    if not token_subject or not rep_subject or not hmac.compare_digest(token_subject, rep_subject):
        logger.warning("SignalWire screened call rejected: invalid rep authorization")
        return _swml(build_hangup_swml())

    browser_call_id = _extract(payload, query, _CALL_ID_KEYS)
    try:
        if screened_single:
            entry_id = _extract(payload, query, _ENTRY_KEYS)
            entry_ids = [str(uuid.UUID(str(entry_id)))]
            line_count = 1
        else:
            raw_entries = _extract(payload, query, ("ParallelEntryIds", "parallel_entry_ids"))
            raw_lines = _extract(payload, query, ("ParallelLineCount", "parallel_line_count"))
            entry_ids = json.loads(raw_entries)
            line_count = int(raw_lines)
            if not isinstance(entry_ids, list) or line_count not in {2, 3, 5, 10}:
                raise ValueError("unsupported line count")
            entry_ids = [str(uuid.UUID(str(value))) for value in entry_ids]
            if len(entry_ids) != line_count or len(set(entry_ids)) != line_count:
                raise ValueError("entry count does not match selected lines")
        if not browser_call_id or len(browser_call_id) > 128:
            raise ValueError("missing browser call id")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning(f"SignalWire screened call rejected: invalid input ({exc})")
        return _swml(build_hangup_swml())

    caller_id = await _resolve_signalwire_caller_id(identity)
    if not caller_id:
        logger.warning("SignalWire screened call rejected: no verified caller ID")
        return _swml(build_hangup_swml())

    try:
        backend_endpoint, _ = await get_backend_endpoints()
        backend_endpoint = str(backend_endpoint or "").rstrip("/")
        if not backend_endpoint.startswith("https://"):
            raise RuntimeError("HTTPS backend endpoint is unavailable")
        entries = await _load_signalwire_parallel_entries(entry_ids)
        if len(entries) != line_count:
            raise RuntimeError("one or more selected leads could not be verified")

        session_id = str(uuid.uuid4())
        conference_name = f"sysevo-par-{session_id}"
        # A batch whose closing callback was lost stays active and the
        # one-active-session rule would refuse this rep's every later batch.
        await signalwire_parallel_store.expire_stale_sessions(rep_user_id=rep_subject)
        if screened_single:
            reserved_legs = await signalwire_parallel_store.start_screened_single_session(
                session_id=session_id,
                rep_user_id=rep_subject,
                rep_call_id=browser_call_id,
                conference_name=conference_name,
                entry_id=entry_ids[0],
            )
        else:
            reserved_legs = await signalwire_parallel_store.start_session(
                session_id=session_id,
                rep_user_id=rep_subject,
                rep_call_id=browser_call_id,
                conference_name=conference_name,
                entry_ids=entry_ids,
                line_count=line_count,
            )
        # Reserve and authorize the exact rows before consulting compliance;
        # an invalid or stolen entry id must not be usable to poll the policy
        # service or create audit records on somebody else's lead.
        try:
            if screened_single:
                caller_ids = {
                    entries[0]["id"]: await _resolve_signalwire_parallel_caller_id(
                        rep_user_id=rep_subject,
                        entry_id=entries[0]["id"],
                        to_number=entries[0]["phone"],
                        fallback_number=caller_id,
                    )
                }
            else:
                caller_ids = await _resolve_signalwire_parallel_caller_ids(
                    rep_user_id=rep_subject,
                    entry_ids=entry_ids,
                    fallback_number=caller_id,
                )
            # Every destination is checked, concurrently: up to ten lines of
            # two lookups each ran one after another while the rep's own
            # SignalWire leg waited on this SWML response. Any block (or an
            # unreachable compliance service) still fails the whole batch.
            await asyncio.gather(*(
                _check_signalwire_parallel_compliance(
                    phone=entry["phone"], provider_call_id=f"sw-preflight-{session_id}-{position}",
                    entry_id=entry["id"], rep_user_id=rep_subject,
                )
                for position, entry in enumerate(entries)
            ))
        except Exception:
            await signalwire_parallel_store.finish_session(
                session_id=session_id, status="failed"
            )
            raise

        first = entries[0]
        if not await create_dialer_call(
            parent_call_sid=browser_call_id,
            rep_user_id=rep_subject,
            entry_id=first["id"],
            from_number=caller_ids[first["id"]],
            to_number=first["phone"],
            provider="signalwire",
        ):
            await signalwire_parallel_store.finish_session(session_id=session_id, status="failed")
            raise RuntimeError("could not persist the SignalWire parent call")

        await _originate_signalwire_batch(
            session_id=session_id, rep_user_id=rep_subject,
            entries=entries, reserved_legs=reserved_legs,
            caller_ids=caller_ids, backend_endpoint=backend_endpoint,
        )

        # The browser leg waits in this conference. Exactly `line_count` PSTN
        # calls were accepted by SignalWire before the rep joins it.
        return _swml(await _signalwire_parallel_join_swml(
            conference_name=conference_name, session_id=session_id, leg_id="rep"
        ))
    except Exception as exc:  # noqa: BLE001 - never fall back to one call
        logger.exception(f"SignalWire parallel batch failed closed: {exc}")
        return _swml(build_hangup_swml())


async def _originate_signalwire_batch(
    *, session_id: str, rep_user_id: str, entries: list[dict],
    reserved_legs: list[dict], caller_ids: dict[str, str], backend_endpoint: str,
) -> None:
    """Start all reserved lines concurrently and clean up every partial success."""
    from api.services.telephony.dialer.signalwire_calling import end_call, originate_call

    started: list[tuple[str, str]] = []

    async def originate(entry: dict, reserved: dict) -> None:
        leg_id = str(reserved["id"])
        result = await originate_call(
            from_number=caller_ids[entry["id"]], to_number=entry["phone"],
            swml_url=_parallel_callback_url(backend_endpoint, "sw-parallel-leg", session_id, leg_id),
            status_url=_parallel_callback_url(backend_endpoint, "sw-parallel-status", session_id, leg_id),
        )
        call_id = str(result["id"])
        # Keep the provider handle before any later write can fail. This also
        # catches a leg whose history or registration failed after origination.
        started.append((leg_id, call_id))
        if not await signalwire_parallel_store.register_provider_call(
            session_id=session_id, leg_id=leg_id, provider_call_id=call_id,
        ):
            raise RuntimeError("could not persist a SignalWire call leg")
        if not await create_dialer_call(
            parent_call_sid=call_id, rep_user_id=rep_user_id,
            entry_id=entry["id"], from_number=caller_ids[entry["id"]],
            to_number=entry["phone"], provider="signalwire",
        ):
            raise RuntimeError("could not persist SignalWire per-line history")

    # Wait for every worker even if one fails, so a late successful originate
    # cannot escape cleanup after the first failure has returned.
    results = await asyncio.gather(*(
        originate(entry, leg) for entry, leg in zip(entries, reserved_legs, strict=True)
    ), return_exceptions=True)
    if not any(isinstance(result, BaseException) for result in results):
        return

    async def cleanup(leg_id: str, call_id: str) -> None:
        try:
            await end_call(call_id=call_id, reason="error")
        except Exception:
            logger.exception("Could not cancel partially originated SignalWire leg {}", call_id)
        await signalwire_parallel_store.update_leg(
            session_id=session_id, leg_id=leg_id, provider_call_id=call_id, status="failed",
        )
        await update_dialer_call_status(
            parent_call_sid=call_id, child_call_sid=None, status="failed",
            duration_seconds=None, rep_user_id=rep_user_id, provider="signalwire",
        )

    await asyncio.gather(*(cleanup(leg_id, call_id) for leg_id, call_id in started), return_exceptions=True)
    await signalwire_parallel_store.finish_session(session_id=session_id, status="failed")
    raise RuntimeError("SignalWire could not start the complete parallel batch")


async def _load_signalwire_parallel_entries(entry_ids: list[str]) -> list[dict[str, str]]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase server credentials are unavailable")
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/cold_call_entries",
            params={"select": "id,phone", "id": f"in.({','.join(entry_ids)})"},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
        )
        response.raise_for_status()
        rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError("Lead lookup returned an invalid response")
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry_id = str(row.get("id") or "")
        phone = normalize_lead_number(str(row.get("phone") or ""))
        if entry_id in entry_ids and phone:
            by_id[entry_id] = {"id": entry_id, "phone": phone}
    return [by_id[entry_id] for entry_id in entry_ids if entry_id in by_id]


async def _cancel_signalwire_ringing_siblings(
    *, session_id: str, winner_leg_id: str, rep_user_id: str,
) -> None:
    """Cancel only siblings still ringing when the database claims cleanup."""
    from api.services.telephony.dialer.signalwire_calling import end_call

    async def cancel(leg: dict) -> None:
        call_id = str(leg.get("provider_call_id") or "")
        if not call_id or leg.get("id") == winner_leg_id or leg.get("status") not in {"queued", "ringing"}:
            return
        try:
            claimed = await signalwire_parallel_store.claim_ringing_cancel(
                session_id=session_id, leg_id=str(leg["id"]), provider_call_id=call_id,
            )
            if not claimed:
                return  # The person answered while the sibling list was in flight.
            await end_call(call_id=call_id, reason="noAnswer")
            await signalwire_parallel_store.record_outcome(
                session_id=session_id, leg_id=str(leg["id"]),
                provider_call_id=call_id, outcome="no_answer",
            )
            await update_dialer_call_status(
                parent_call_sid=call_id, child_call_sid=None, status="no-answer",
                duration_seconds=None, rep_user_id=rep_user_id, provider="signalwire",
            )
        except Exception:
            logger.exception("Could not cancel losing SignalWire leg {}", call_id)

    try:
        legs = await signalwire_parallel_store.get_session_legs(session_id=session_id)
        await asyncio.gather(*(cancel(leg) for leg in legs))
    except Exception:
        logger.exception("Could not enumerate losing SignalWire parallel legs")


async def _resolve_signalwire_parallel_caller_id(
    *, rep_user_id: str, entry_id: str, to_number: str, fallback_number: str
) -> str:
    """Resolve per-lead local presence with the rep line as the safe fallback."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase caller-ID lookup is unavailable")
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/dialer_resolve_outbound_caller_id",
            json={
                "p_user_id": rep_user_id,
                "p_to_number": to_number,
                "p_provider": "signalwire",
                "p_entry_id": entry_id,
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        rows = response.json()
    if not isinstance(rows, list) or len(rows) > 1:
        raise RuntimeError("Caller-ID lookup returned an invalid result")
    if not rows:
        return fallback_number
    row = rows[0]
    number = normalize_lead_number(str(row.get("phone_number") or ""))
    if not number or row.get("provider") != "signalwire":
        raise RuntimeError("Caller-ID lookup returned a non-SignalWire number")
    if number != fallback_number and not await _is_signalwire_owned_number(number):
        raise RuntimeError("Local-presence caller ID is not verified as SignalWire-owned")
    return number


async def _resolve_signalwire_parallel_caller_ids(
    *, rep_user_id: str, entry_ids: list[str], fallback_number: str
) -> dict[str, str]:
    """Resolve a whole batch together so available local DIDs are not reused."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase caller-ID lookup is unavailable")
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/dialer_resolve_parallel_caller_ids",
            json={
                "p_user_id": rep_user_id,
                "p_entry_ids": entry_ids,
                "p_provider": "signalwire",
                "p_fallback_number": fallback_number,
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        rows = response.json()
    if not isinstance(rows, list) or len(rows) != len(entry_ids):
        raise RuntimeError("Parallel caller-ID lookup returned an incomplete batch")

    by_entry: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Parallel caller-ID lookup returned an invalid row")
        entry_id = str(row.get("entry_id") or "")
        number = normalize_lead_number(str(row.get("phone_number") or ""))
        if (
            entry_id not in entry_ids
            or entry_id in by_entry
            or not number
            or row.get("provider") != "signalwire"
        ):
            raise RuntimeError("Parallel caller-ID lookup returned an invalid assignment")
        by_entry[entry_id] = number

    if set(by_entry) != set(entry_ids):
        raise RuntimeError("Parallel caller-ID lookup did not cover the selected leads")
    unique_non_fallback = {number for number in by_entry.values() if number != fallback_number}
    ownership = await asyncio.gather(*(
        _is_signalwire_owned_number(number) for number in unique_non_fallback
    ))
    if not all(ownership):
        raise RuntimeError("Parallel caller-ID pool contains a number SignalWire does not own")
    return by_entry


async def _check_signalwire_parallel_compliance(
    *, phone: str, provider_call_id: str, entry_id: str, rep_user_id: str
) -> None:
    del provider_call_id, rep_user_id  # retained in the signature for existing audit call sites
    block_reason = await phone_block_reason(phone)
    if block_reason:
        raise RuntimeError(f"Dial blocked by server suppression check: {block_reason}")
    allowed, reason = await calling_hours_allowed(phone, entry_id)
    if not allowed:
        raise RuntimeError(f"Dial blocked by server calling-hours check: {reason}")


async def _load_signalwire_parallel_default_drop(
    rep_user_id: str, entry_id: str | None = None, parent_call_sid: str | None = None,
) -> dict[str, str] | None:
    """The recording this call will play, from the same resolver as Twilio.

    Explicit team choice, then the rep's own default, then the account team
    default for the lead's account. The owner-folder check is enforced in
    the shared resolver; only provider-playable files (WAV/MP3) qualify.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase voicemail credentials are unavailable")
    drop = await resolve_effective_voicemail_drop(
        rep_user_id=rep_user_id, entry_id=entry_id, parent_call_sid=parent_call_sid,
    )
    if not drop:
        return None
    path = str(drop.get("storage_path") or "")
    drop_id = str(drop.get("id") or "")
    try:
        uuid.UUID(drop_id)
    except (ValueError, TypeError, AttributeError):
        return None
    if not is_provider_playable_voicemail(path):
        logger.warning(f"SignalWire cannot play voicemail recording {drop_id} (not WAV/MP3)")
        return None
    return {"id": drop_id, "storage_path": path}


async def _signed_signalwire_voicemail_url(storage_path: str) -> str:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase storage credentials are unavailable")
    encoded_path = quote(storage_path, safe="/")
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(
            f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/sign/voicemail-drops/{encoded_path}",
            json={"expiresIn": 180},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        result = response.json()
    signed = str(result.get("signedURL") or "") if isinstance(result, dict) else ""
    if signed.startswith("https://"):
        base = SUPABASE_URL.rstrip("/")
        if signed.startswith(base + "/"):
            return signed
        raise RuntimeError("Voicemail storage returned an unexpected signed URL origin")
    if signed.startswith("/object/"):
        return f"{SUPABASE_URL.rstrip('/')}/storage/v1{signed}"
    raise RuntimeError("Voicemail storage did not return a valid signed URL")


def _parallel_callback_url(
    backend_endpoint: str, path: str, session_id: str, leg_id: str
) -> str:
    nonce = f"sw-parallel:{session_id}:{leg_id}"
    base = _webhook_url(backend_endpoint, path, nonce)
    if not base:
        raise RuntimeError("SignalWire callback URL could not be created")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'session_id': session_id, 'leg_id': leg_id})}"


def _parallel_query_ids(query, *, allow_rep: bool = False) -> tuple[str, str] | None:
    """The session and leg a parallel callback is about, bound to its signature.

    The per-call HMAC signs only `call_id` (the `sw-parallel:{session}:{leg}`
    nonce); `session_id` and `leg_id` ride beside it unsigned. Without this
    check one leaked callback URL could be replayed with another leg's ids.
    """
    session_id = str(query.get("session_id") or "")
    leg_id = str(query.get("leg_id") or "")
    try:
        uuid.UUID(session_id)
        if not (allow_rep and leg_id == "rep"):
            uuid.UUID(leg_id)
    except (ValueError, TypeError, AttributeError):
        return None
    if str(query.get("call_id") or "") != f"sw-parallel:{session_id}:{leg_id}":
        return None
    return session_id, leg_id


# Leg states after which SignalWire will not move the line again. Must match
# dialer_parallel_batch_summary's notion of a settled line; `abandoned_early`
# (a sibling cancelled within six seconds of ringing) was missing here, so a
# batch containing one never finished, and the rep's next batch was refused as
# "rep already has an active SignalWire parallel session".
_TERMINAL_PARALLEL_LEG_STATES = frozenset({
    "voicemail", "busy", "no-answer", "failed", "cancelled", "abandoned_early", "completed",
})


# Lines whose dialer_calls history already carries their final result.
_SETTLED_HISTORY_LEG_STATES = frozenset({
    "voicemail", "busy", "no-answer", "failed", "cancelled", "abandoned_early",
})


async def _finish_signalwire_batch_if_settled(*, session_id: str, session: dict) -> bool:
    """Close the batch and the rep's waiting leg once no line needs the rep.

    A line playing the rep's voicemail (`voicemail_playing`) needs nothing more
    from the rep: it finishes by itself and its play callback records the
    result. It counts as settled, so the rep is released immediately instead of
    waiting for every message to finish.
    """
    all_legs = await signalwire_parallel_store.get_session_legs(session_id=session_id)
    if not all_legs or not all(
        row.get("status") in _TERMINAL_PARALLEL_LEG_STATES or row.get("status") == "voicemail_playing"
        for row in all_legs
    ):
        return False
    finished = await signalwire_parallel_store.finish_session(
        session_id=session_id,
        status="completed" if session.get("winner_leg_id") else "failed",
    )
    if not finished:
        return False  # Another callback (or the rep leaving) already closed it.
    rep_call_id = str(session.get("rep_call_id") or "")
    if (
        rep_call_id
        and int(session.get("line_count") or 0) == 1
        and any(row.get("status") == "voicemail_playing" for row in all_legs)
    ):
        # The browser's wrap-up reads this to open straight onto "Voicemail"
        # and move to the next lead (the leg's play callback writes the outcome).
        await update_signalwire_dialer_amd(parent_call_sid=rep_call_id, voicemail_started=True)
    # Ending the rep's conference leg closes the active call screen when every
    # prospect leg is done, including an empty batch.
    from api.services.telephony.dialer.signalwire_calling import end_call
    try:
        await end_call(call_id=str(session.get("rep_call_id") or ""), reason="hangup")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Could not close the rep leg after the parallel batch: {exc}")
    return True


async def _abandon_signalwire_batch_after_rep_left(*, session_id: str) -> None:
    """The rep's leg left the batch room: stop every line that could still reach it.

    The rep's conference ends with them (end_on_exit), so a prospect who answers
    afterwards would be claimed as the winner and dropped into an empty room --
    an abandoned call. Closing the session first makes every later AMD decision
    and leg bootstrap hang up; ringing lines are then cancelled.
    """
    session = await signalwire_parallel_store.get_session(session_id=session_id)
    if not session:
        return
    if await signalwire_parallel_store.session_rep_released(session_id=session_id):
        # The rep left on purpose after queuing their voicemail: the leg stays
        # with the server until its AMD decides (machine -> recording, person
        # -> hang up). Cancelling it here would lose the message.
        return
    if session.get("status") == "dialing":
        await signalwire_parallel_store.finish_session(session_id=session_id, status="cancelled")
    await _cancel_signalwire_ringing_siblings(
        session_id=session_id,
        winner_leg_id=str(session.get("winner_leg_id") or ""),
        rep_user_id=str(session.get("rep_user_id") or ""),
    )


async def _settle_unreported_voicemail_playback(
    *, session_id: str, leg_id: str, provider_call_id: str, session: dict, delay_seconds: float = 15.0,
) -> None:
    """Don't let a lost play callback hold the batch open for ever.

    SignalWire documents play status callbacks as best-effort. A machine leg left
    in `voicemail_playing` after its call ended kept the session active, which
    blocks the rep's next batch. The delivery is unknown, so it is recorded as a
    detected voicemail rather than a sent one.
    """
    await asyncio.sleep(delay_seconds)
    try:
        context = await signalwire_parallel_store.get_leg_context(session_id=session_id, leg_id=leg_id)
        if not context or context["leg"].get("status") != "voicemail_playing":
            return
        if not await signalwire_parallel_store.update_leg(
            session_id=session_id, leg_id=leg_id,
            provider_call_id=provider_call_id, status="voicemail",
        ):
            return
        logger.warning(f"SignalWire leg {leg_id} ended without a playback result; recorded as voicemail")
        await signalwire_parallel_store.record_outcome(
            session_id=session_id, leg_id=leg_id,
            provider_call_id=provider_call_id, outcome="voicemail",
        )
        await update_dialer_call_status(
            parent_call_sid=provider_call_id, child_call_sid=None,
            status="completed", duration_seconds=None,
            rep_user_id=session.get("rep_user_id"), provider="signalwire",
        )
        await _finish_signalwire_batch_if_settled(session_id=session_id, session=context["session"])
    except Exception:  # noqa: BLE001 - background cleanup must not raise
        logger.exception("Could not settle an unreported SignalWire voicemail playback")


async def _signalwire_parallel_join_swml(
    *, conference_name: str, session_id: str, leg_id: str
) -> dict:
    """Attach an authenticated conference participant callback to a call leg."""
    backend_endpoint, _ = await get_backend_endpoints()
    callback_url = _parallel_callback_url(
        str(backend_endpoint or "").rstrip("/"),
        "sw-parallel-conference", session_id, leg_id,
    )
    return build_signalwire_parallel_rep_swml(
        conference_name=conference_name, status_callback_url=callback_url
    )


def _signalwire_event_timestamp(payload: dict) -> str | None:
    """Normalize SignalWire's documented Unix event timestamp to UTC ISO."""
    raw = payload.get("timestamp")
    if isinstance(raw, (int, float)) and 0 < raw < 10_000_000_000:
        return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
    return None


@router.get("/signalwire/parallel-auth-token")
async def get_signalwire_parallel_auth_token(user: UserModel = Depends(require_sales_dialer_role)):
    """A fresh rep proof for ONE SignalWire parallel/screened dial.

    The proof lives five minutes, but the browser only received it with the
    softphone credentials at registration (and at rotation boundaries); the
    SDK's credential refresh discards it. Any parallel dial placed more than
    five minutes after the page connected therefore carried an expired proof
    and was hung up by _start_signalwire_session. The browser fetches this
    immediately before dialling instead.
    """
    from fastapi import HTTPException
    from api.services.telephony.dialer.parallel_auth import issue_parallel_auth_token

    token = issue_parallel_auth_token(str(user.provider_id or ""))
    if not token:
        raise HTTPException(status_code=503, detail="Parallel dialing authorization is not configured.")
    return {"parallel_auth_token": token}


# A screened leg the server's AMD has not classified yet (ringing or greeting).
_PRE_DECISION_LEG_STATES = frozenset({"reserved", "queued", "ringing", "screening"})


def _signalwire_drop_state(found: dict) -> dict:
    """Server truth for the rep's one-line screened call, in the Twilio shape."""
    session, leg = found["session"], found["leg"]
    status = str(leg.get("status") or "")
    machine = leg.get("amd_result") == "machine"
    if leg.get("voicemail_sent_at") and status in {"voicemail", "completed"}:
        state = "sent"
    elif status == "voicemail_playing":
        state = "playing"
    elif leg.get("voicemail_started_at") and status == "failed":
        state = "failed"
    elif leg.get("manual_voicemail_requested_at") and status in _PRE_DECISION_LEG_STATES | {"machine"}:
        state = "waiting"
    elif leg.get("manual_voicemail_requested_at"):
        # Queued, then a person (or no mailbox) answered: nothing was played.
        state = "rejected"
    else:
        state = "idle"
    bridged = str(session.get("winner_leg_id") or "") == str(leg.get("id") or "") and status == "connected"
    result = {
        "state": state,
        # machine_end_other: a mailbox the server's AMD bridged to the rep.
        "amd_result": "machine_end_other" if machine else leg.get("amd_result"),
        "ready": bool(machine and bridged),
    }
    if state == "failed":
        result["failure"] = "playback_incomplete"
    return result


class SignalWireVoicemailDropRequest(BaseModel):
    rep_call_id: str = Field(min_length=8, max_length=128)
    action: Literal["send", "cancel"] = "send"


@router.get("/dialer/signalwire/voicemail-drop/{rep_call_id}")
async def signalwire_voicemail_drop_status(
    rep_call_id: str, user: UserModel = Depends(require_sales_dialer_role),
):
    """Mailbox/playback state of this rep's own screened SignalWire queue call."""
    from fastapi import HTTPException
    rep_user_id = str(getattr(user, "provider_id", "") or "")
    if not rep_user_id or not (8 <= len(rep_call_id) <= 128):
        raise HTTPException(status_code=404, detail="This SignalWire call was not found for your account.")
    try:
        found = await signalwire_parallel_store.get_screened_single_for_rep(
            rep_call_id=rep_call_id, rep_user_id=rep_user_id,
        )
    except signalwire_parallel_store.SignalWireParallelStoreError:
        raise HTTPException(status_code=503, detail="Could not read this call. Try again.")
    if not found:
        # Keypad / direct calls have no server-side mailbox screening.
        raise HTTPException(status_code=404, detail="This SignalWire call was not found for your account.")
    return _signalwire_drop_state(found)


@router.post("/dialer/signalwire/voicemail-drop")
async def signalwire_send_voicemail(
    body: SignalWireVoicemailDropRequest, user: UserModel = Depends(require_sales_dialer_role),
):
    """The rep's one tap on "Send my voicemail" for a bridged mailbox.

    The prospect leg is moved out of the rep's conference onto a play-and-hang-up
    SWML (Calling API `update`), and the rep is released at once. The leg's play
    callback records sent/failed and the outcome exactly as an automatic drop.
    """
    from fastapi import HTTPException
    from api.services.telephony.dialer.signalwire_calling import SignalWireCallError, update_call_swml

    rep_user_id = str(getattr(user, "provider_id", "") or "")
    if not rep_user_id:
        raise HTTPException(status_code=503, detail="Your account is missing its dialer user mapping.")
    try:
        found = await signalwire_parallel_store.get_screened_single_for_rep(
            rep_call_id=body.rep_call_id, rep_user_id=rep_user_id,
        )
    except signalwire_parallel_store.SignalWireParallelStoreError:
        raise HTTPException(status_code=503, detail="Could not read this call. The call was left unchanged.")
    if not found:
        raise HTTPException(status_code=404, detail="This SignalWire call was not found for your account.")
    current = _signalwire_drop_state(found)
    if body.action == "cancel" or current["state"] in {"playing", "sent", "waiting"}:
        # Once the rep has handed the line over it belongs to the server.
        return {"state": current["state"]}
    session, leg = found["session"], found["leg"]
    if (
        str(leg.get("status") or "") in _PRE_DECISION_LEG_STATES
        and session.get("status") == "dialing"
    ):
        # Pressed while it rings or during the greeting: queue it on the leg,
        # release the rep now, and let the AMD decision play it on a machine
        # (or hang up on a person) in the background.
        try:
            queued = await signalwire_parallel_store.request_manual_voicemail(
                session_id=str(session["id"]), leg_id=str(leg["id"]), rep_user_id=rep_user_id,
            )
        except signalwire_parallel_store.SignalWireParallelStoreError:
            raise HTTPException(status_code=503, detail="Could not queue the voicemail. The call was left unchanged.")
        if queued != "waiting":
            raise HTTPException(status_code=409, detail="This call can no longer take a voicemail.")
        from api.services.telephony.dialer.signalwire_calling import end_call
        try:
            await end_call(call_id=body.rep_call_id, reason="hangup")
        except SignalWireCallError as exc:  # the request stands; the browser hangs up its own leg too
            logger.warning(f"Could not end the rep leg {body.rep_call_id} after queuing a voicemail: {exc}")
        return {"state": "waiting", "released": True}
    if not current["ready"]:
        raise HTTPException(status_code=409, detail="The dialer has not confirmed a voicemail on this call, so nothing was sent.")

    session_id, leg_id = str(session["id"]), str(leg["id"])
    provider_call_id = str(leg.get("provider_call_id") or "")
    drop = await _load_signalwire_parallel_default_drop(
        rep_user_id, entry_id=str(leg.get("entry_id") or "") or None, parent_call_sid=body.rep_call_id,
    )
    if not drop:
        raise HTTPException(status_code=409, detail="Record a WAV voicemail before sending one from SignalWire.")
    try:
        recording_url = await _signed_signalwire_voicemail_url(drop["storage_path"])
        backend_endpoint, _ = await get_backend_endpoints()
        playback_url = _parallel_callback_url(
            str(backend_endpoint or "").rstrip("/"), "sw-parallel-playback", session_id, leg_id,
        )
    except Exception as exc:  # noqa: BLE001 - nothing has changed on the call yet
        logger.error(f"Could not prepare the SignalWire voicemail for {body.rep_call_id}: {exc}")
        raise HTTPException(status_code=503, detail="Could not load your recording. Speak your message instead.")
    if not await signalwire_parallel_store.claim_manual_voicemail(
        session_id=session_id, leg_id=leg_id, provider_call_id=provider_call_id,
        drop_id=drop["id"], rep_user_id=rep_user_id,
    ):
        refreshed = await signalwire_parallel_store.get_screened_single_for_rep(
            rep_call_id=body.rep_call_id, rep_user_id=rep_user_id,
        )
        state = _signalwire_drop_state(refreshed)["state"] if refreshed else "idle"
        if state in {"playing", "sent"}:
            return {"state": state}  # A double tap: the first one is already playing.
        raise HTTPException(status_code=409, detail="The voicemail could not be safely started.")

    from api.services.telephony.dialer.swml import build_signalwire_parallel_voicemail_swml
    try:
        await update_call_swml(call_id=provider_call_id, swml=build_signalwire_parallel_voicemail_swml(
            recording_url=recording_url, playback_status_url=playback_url,
        ))
    except SignalWireCallError as exc:
        logger.error(f"SignalWire refused to start the voicemail on {provider_call_id}: {exc}")
        await signalwire_parallel_store.release_manual_voicemail(
            session_id=session_id, leg_id=leg_id, provider_call_id=provider_call_id,
        )
        raise HTTPException(status_code=502, detail="SignalWire could not start the message. Speak your message instead.")
    await update_signalwire_dialer_amd(parent_call_sid=provider_call_id, voicemail_started=True)
    # Releases the rep (and closes the session, so their next dial is allowed)
    # while the prospect leg keeps playing.
    _spawn_background(_finish_signalwire_batch_if_settled(
        session_id=session_id, session={**session, "line_count": 1},
    ))
    return {"state": "playing", "released": True}


@router.post("/sw-parallel-conference", include_in_schema=False)
async def handle_sw_parallel_conference(request: Request):
    """Persist real rep/prospect conference joins for the parallel connect guard."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-parallel-conference", request, payload)
        if not _secret_ok("sw-parallel-conference", request, allow_per_call_signature=True):
            return JSONResponse({"ok": False}, status_code=403)
        query = request.query_params
        ids = _parallel_query_ids(query, allow_rep=True)
        if not ids:
            return JSONResponse({"ok": False}, status_code=400)
        session_id, leg_id = ids
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        provider_call_id = str(params.get("call_id") or "")
        conference_name = str(params.get("name") or "")
        if params.get("status") == "participant-leave" and leg_id == "rep":
            # Only the rep's own leg leaving its own batch room cancels the batch.
            session = await signalwire_parallel_store.get_session(session_id=session_id)
            if (
                not session or session.get("conference_name") != conference_name
                or not provider_call_id or session.get("rep_call_id") != provider_call_id
            ):
                return JSONResponse({"ok": False}, status_code=409)
            if session.get("status") not in {"dialing", "connected"}:
                return JSONResponse({"ok": True, "ignored": True})
            await _abandon_signalwire_batch_after_rep_left(session_id=session_id)
            return JSONResponse({"ok": True})
        if params.get("status") != "participant-join":
            return JSONResponse({"ok": True, "ignored": True})
        event_at = _signalwire_event_timestamp(payload)
        if not provider_call_id or not event_at:
            return JSONResponse({"ok": False, "reason": "missing_event_identity_or_timestamp"}, status_code=400)

        session = await signalwire_parallel_store.get_session(session_id=session_id)
        if not session or session.get("conference_name") != conference_name:
            return JSONResponse({"ok": False}, status_code=409)
        if session.get("rep_call_id") == provider_call_id:
            kind = "rep_join"
            timing_leg_id = None
        else:
            legs = await signalwire_parallel_store.get_session_legs(session_id=session_id)
            matching = next((row for row in legs if row.get("provider_call_id") == provider_call_id), None)
            if not matching or str(matching.get("id")) != str(session.get("winner_leg_id")):
                return JSONResponse({"ok": False}, status_code=409)
            kind = "lead_join"
            timing_leg_id = str(matching["id"])
        recorded = await signalwire_parallel_store.record_parallel_timing(
            session_id=session_id, leg_id=timing_leg_id,
            provider_call_id=provider_call_id, event_kind=kind,
            event_at=event_at,
        )
        return JSONResponse({"ok": recorded, "ignored": not recorded})
    except Exception as exc:  # noqa: BLE001 - provider callbacks must be non-fatal
        logger.exception(f"SignalWire parallel conference callback failed: {exc}")
        return JSONResponse({"ok": False}, status_code=500)


@router.post("/sw-overflow-conference", include_in_schema=False)
async def handle_sw_overflow_conference(request: Request):
    """Track a separate teammate conference using bound participant identities."""
    try:
        payload = await _read_payload(request)
        if not _secret_ok("sw-overflow-conference", request, allow_per_call_signature=True):
            return JSONResponse({"ok": False}, status_code=403)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        conference_name = str(params.get("name") or "")
        # The HMAC is scoped to this conference. A valid URL for another room
        # cannot write its connection times or release its rep reservation.
        if (
            not re.fullmatch(r"sysevo-overflow-[0-9a-f-]{36}", conference_name)
            or request.query_params.get("call_id") != conference_name
        ):
            return JSONResponse({"ok": False}, status_code=409)
        participant = str(params.get("call_id") or "")
        event = params.get("status")
        if event == "participant-join":
            event_at = _signalwire_event_timestamp(payload)
            if not participant or not event_at:
                return JSONResponse({"ok": False}, status_code=400)
            recorded = await record_overflow_join(
                conference_name=conference_name, provider="signalwire",
                participant_call_id=participant, event_at=event_at,
            )
        elif event == "participant-leave" and participant:
            recorded = await close_overflow_conference(
                conference_name=conference_name, provider="signalwire",
                participant_call_id=participant,
            )
        else:
            return JSONResponse({"ok": True, "ignored": True})
        if recorded is None:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": recorded, "ignored": not recorded})
    except Exception as exc:  # noqa: BLE001 - let the provider retry transient failures
        logger.exception(f"SignalWire overflow conference callback failed: {exc}")
        return JSONResponse({"ok": False}, status_code=500)


async def _signalwire_overflow_callback_url(conference_name: str) -> str:
    backend_endpoint, _ = await get_backend_endpoints()
    return _webhook_url(
        str(backend_endpoint or "").rstrip("/"), "sw-overflow-conference", conference_name,
    )


@router.post("/sw-parallel-leg", include_in_schema=False)
async def handle_sw_parallel_leg(request: Request):
    """Return AMD SWML only for a reserved, authenticated batch leg."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-parallel-leg", request, payload)
        if not _secret_ok("sw-parallel-leg", request, allow_per_call_signature=True):
            return _swml(build_hangup_swml())
        query = request.query_params
        ids = _parallel_query_ids(query)
        if not ids:
            return _swml(build_hangup_swml())
        session_id, leg_id = ids

        provider_call_id = _extract(payload, dict(query), _CALL_ID_KEYS)
        if not provider_call_id or provider_call_id.startswith("sw-parallel:"):
            return _swml(build_hangup_swml())
        context = await signalwire_parallel_store.get_leg_context(
            session_id=session_id, leg_id=leg_id
        )
        # Other PSTN lines can answer after the first human has already made
        # the batch connected. They still need AMD so the decision callback
        # can route a second human to the configured overflow teammate. The
        # decision handler performs the atomic winner/overflow claim; this
        # bootstrap only rejects sessions that are already terminal.
        if not context or context["session"].get("status") not in {"dialing", "connected"}:
            return _swml(build_hangup_swml())
        leg = context["leg"]
        if leg.get("provider_call_id") is None:
            registered = await signalwire_parallel_store.register_provider_call(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id,
            )
            if not registered:
                return _swml(build_hangup_swml())
        elif leg.get("provider_call_id") != provider_call_id:
            return _swml(build_hangup_swml())

        # Fetching this SWML means the prospect has already answered. Mark it
        # before AMD so a simultaneous winner cannot claim it as ringing cleanup.
        if leg.get("status") in {"reserved", "queued", "ringing"}:
            if not await signalwire_parallel_store.update_leg(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, status="screening",
            ):
                return _swml(build_hangup_swml())

        session = context["session"]
        policy = await signalwire_parallel_store.get_voicemail_policy(
            rep_user_id=str(session["rep_user_id"]),
            entry_id=str(leg["entry_id"]),
            parent_call_sid=str(session["rep_call_id"]),
        )
        if not policy:
            logger.error("SignalWire parallel leg has no effective voicemail/AMD policy")
            return _swml(build_hangup_swml())
        backend_endpoint, _ = await get_backend_endpoints()
        decision_url = _parallel_callback_url(
            str(backend_endpoint or "").rstrip("/"),
            "sw-parallel-decide", session_id, leg_id,
        )
        return _swml(build_signalwire_parallel_leg_swml(
            decision_url=decision_url,
            amd_preset=str(policy.get("machine_detection_preset") or "balanced"),
            machine_detection_enabled=(
                int(session.get("line_count") or 0) > 1
                or policy.get("machine_detection_enabled") is True
            ),
        ))
    except Exception as exc:  # noqa: BLE001 - a failed bootstrap must not bridge
        logger.exception(f"SignalWire parallel leg bootstrap failed closed: {exc}")
        return _swml(build_hangup_swml())


@router.post("/sw-parallel-status", include_in_schema=False)
async def handle_sw_parallel_status(request: Request):
    """Persist provider status for a specific SignalWire parallel prospect leg."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-parallel-status", request, payload)
        if not _secret_ok("sw-parallel-status", request, allow_per_call_signature=True):
            return JSONResponse({"ok": False}, status_code=403)
        query = request.query_params
        ids = _parallel_query_ids(query)
        if not ids:
            return JSONResponse({"ok": False}, status_code=400)
        session_id, leg_id = ids
        context = await signalwire_parallel_store.get_leg_context(
            session_id=session_id, leg_id=leg_id
        )
        if not context:
            return JSONResponse({"ok": False}, status_code=404)
        session, leg = context["session"], context["leg"]
        provider_call_id = _extract(payload, dict(query), _CALL_ID_KEYS)
        if not provider_call_id or provider_call_id.startswith("sw-parallel:"):
            return JSONResponse({"ok": False}, status_code=400)
        if leg.get("provider_call_id") is None:
            await signalwire_parallel_store.register_provider_call(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id,
            )
        elif leg.get("provider_call_id") != provider_call_id:
            return JSONResponse({"ok": False}, status_code=409)

        raw_state = (_extract(payload, dict(query), _STATE_KEYS) or "").strip().lower()
        raw_reason = (_extract(payload, dict(query), _END_REASON_KEYS) or "").strip().lower()
        if raw_state in {"created", "queued"}:
            leg_status, call_status = "queued", "initiated"
        elif raw_state in {"ringing", "ring"}:
            leg_status, call_status = "ringing", "ringing"
        elif raw_state in {"answered", "in-progress", "screening"}:
            leg_status, call_status = "screening", "in-progress"
        elif raw_state in {"canceled", "cancelled"}:
            leg_status, call_status = "cancelled", "cancelled"
        elif raw_state in {"failed", "error"}:
            leg_status, call_status = "failed", "failed"
        elif raw_state in {"ended", "completed", "hangup"}:
            reason = re.sub(r"[^a-z]", "", raw_reason)
            if leg.get("status") == "voicemail_playing":
                # The play.status_url callback is authoritative for whether the
                # recording finished. Do not let the subsequent call-ended
                # callback erase a completed voicemail or guess delivery. The
                # play callback is best-effort, though, so settle the line
                # later if it never reports.
                _spawn_background(_settle_unreported_voicemail_playback(
                    session_id=session_id, leg_id=leg_id,
                    provider_call_id=provider_call_id, session=session,
                ))
                return JSONResponse({"ok": True, "awaiting_playback_result": True})
            elif reason in {"busy", "userbusy"}:
                leg_status, call_status = "busy", "busy"
            elif reason in {"noanswer", "timeout"}:
                leg_status, call_status = "no-answer", "no-answer"
            elif reason in {"failed", "error"}:
                leg_status, call_status = "failed", "failed"
            else:
                leg_status, call_status = "completed", "completed"
        else:
            return JSONResponse({"ok": True, "ignored": True})

        if raw_state in {"answered", "in-progress", "screening"}:
            event_at = _signalwire_event_timestamp(payload)
            if event_at:
                await signalwire_parallel_store.record_parallel_timing(
                    session_id=session_id, leg_id=leg_id,
                    provider_call_id=provider_call_id, event_kind="answer",
                    event_at=event_at,
                )

        terminal_event = raw_state in {"ended", "completed", "hangup", "canceled", "cancelled", "failed", "error"}
        if terminal_event:
            # Overflow legs are already dispositioned when the offer is made.
            # Their later end callback can be a no-op in the leg state machine,
            # but it must still release the teammate's reservation.
            await close_inbound_call(provider_call_id=provider_call_id)
            await evaluate_overflow_guard(provider_call_id=provider_call_id, provider="signalwire")

        leg_updated = await signalwire_parallel_store.update_leg(
            session_id=session_id, leg_id=leg_id,
            provider_call_id=provider_call_id, status=leg_status,
        )
        if not leg_updated:
            # Provider status callbacks can arrive after AMD has already routed
            # this human to a teammate. Do not let a stale `answered` event
            # resurrect the terminal overflow leg or its call-history state.
            #
            # An overflow leg's history was left `in-progress` at the offer,
            # so its end still has to be written. A line that already has its
            # final result (a cancelled loser recorded as no-answer, busy,
            # failed, voicemail) must keep it: SignalWire's `ended` for a
            # cancelled sibling would otherwise rewrite it as `completed`.
            if terminal_event and leg.get("status") not in _SETTLED_HISTORY_LEG_STATES:
                await update_dialer_call_status(
                    parent_call_sid=provider_call_id, child_call_sid=None,
                    status=call_status, duration_seconds=None,
                    rep_user_id=session.get("rep_user_id"), provider="signalwire",
                )
            if terminal_event:
                # The last callback of a batch can be a stale one; it must still
                # be able to close the batch.
                await _finish_signalwire_batch_if_settled(session_id=session_id, session=session)
            return JSONResponse({"ok": True, "ignored": True, "reason": "stale_state"})
        if leg_status in {"busy", "no-answer"}:
            await signalwire_parallel_store.record_outcome(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id,
                outcome="busy" if leg_status == "busy" else "no_answer",
            )
        await update_dialer_call_status(
            parent_call_sid=provider_call_id,
            child_call_sid=None,
            status=call_status,
            duration_seconds=None,
            rep_user_id=session.get("rep_user_id"),
            provider="signalwire",
        )
        await _finish_signalwire_batch_if_settled(session_id=session_id, session=session)
        return JSONResponse({"ok": True})
    except Exception as exc:  # noqa: BLE001 - provider callbacks must not raise
        logger.exception(f"SignalWire parallel status update failed: {exc}")
        return JSONResponse({"ok": False}, status_code=500)


@router.post("/sw-parallel-playback", include_in_schema=False)
async def handle_sw_parallel_playback(request: Request):
    """Record whether a machine voicemail drop actually finished playing."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-parallel-playback", request, payload)
        if not _secret_ok("sw-parallel-playback", request, allow_per_call_signature=True):
            return JSONResponse({"ok": False}, status_code=403)

        query = request.query_params
        ids = _parallel_query_ids(query)
        if not ids:
            return JSONResponse({"ok": False}, status_code=400)
        session_id, leg_id = ids
        context = await signalwire_parallel_store.get_leg_context(
            session_id=session_id, leg_id=leg_id
        )
        if not context:
            return JSONResponse({"ok": False}, status_code=404)
        session, leg = context["session"], context["leg"]
        provider_call_id = _extract(payload, dict(query), _CALL_ID_KEYS)
        if not provider_call_id or provider_call_id != leg.get("provider_call_id"):
            return JSONResponse({"ok": False}, status_code=409)

        playback_state = (_extract(
            payload, dict(query), ("play_state", "playState", "play_status", "playStatus", "state", "status")
        ) or "").strip().lower()
        if playback_state in {"finished", "completed", "success"}:
            completed = await signalwire_parallel_store.complete_voicemail_drop(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id,
            )
            if not completed:
                return JSONResponse({"ok": False}, status_code=409)
            await update_signalwire_dialer_amd(
                parent_call_sid=provider_call_id,
                voicemail_sent=True,
            )
            await update_dialer_call_status(
                parent_call_sid=provider_call_id, child_call_sid=None,
                status="completed", duration_seconds=None,
                rep_user_id=session.get("rep_user_id"), provider="signalwire",
            )
        elif playback_state in {"error", "failed", "stopped"}:
            await signalwire_parallel_store.update_leg(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, status="failed",
            )
            await update_dialer_call_status(
                parent_call_sid=provider_call_id, child_call_sid=None,
                status="failed", duration_seconds=None,
                rep_user_id=session.get("rep_user_id"), provider="signalwire",
            )
        else:
            return JSONResponse({"ok": True, "ignored": True})

        await _finish_signalwire_batch_if_settled(session_id=session_id, session=session)
        return JSONResponse({"ok": True})
    except Exception as exc:  # noqa: BLE001 - provider callbacks must not raise
        logger.exception(f"SignalWire voicemail playback callback failed: {exc}")
        return JSONResponse({"ok": False}, status_code=500)


@router.post("/sw-parallel-decide", include_in_schema=False)
async def handle_sw_parallel_decision(request: Request):
    """Persist AMD and atomically bridge or route each detected human.

    A signed per-leg nonce authenticates this callback. The first human joins
    the rep's private conference; a later human gets a separate teammate offer.
    """
    try:
        payload = await _read_payload(request)
        _log_payload("sw-parallel-decide", request, payload)
        if not _secret_ok("sw-parallel-decide", request, allow_per_call_signature=True):
            return _swml(build_hangup_swml())
        query = request.query_params
        ids = _parallel_query_ids(query)
        if not ids:
            return _swml(build_hangup_swml())
        session_id, leg_id = ids

        context = await signalwire_parallel_store.get_leg_context(
            session_id=session_id, leg_id=leg_id
        )
        if not context:
            return _swml(build_hangup_swml())
        session, leg = context["session"], context["leg"]
        conference_name = str(session.get("conference_name") or "")
        if not re.fullmatch(r"sysevo-par-[0-9a-f-]{36}", conference_name):
            logger.error("SignalWire parallel decision refused an invalid conference name")
            return _swml(build_hangup_swml())
        provider_call_id = _extract(payload, dict(query), _CALL_ID_KEYS)
        if not provider_call_id or provider_call_id != leg.get("provider_call_id"):
            logger.warning("SignalWire parallel AMD callback rejected: leg call id mismatch")
            return _swml(build_hangup_swml())

        # Execute callbacks can be retried. Replaying the winner's decision
        # must keep it connected; a late callback for a finished batch must
        # never resurrect a conference leg.
        if session.get("status") == "connected":
            if session.get("winner_leg_id") == leg_id and leg.get("status") == "connected":
                return _swml(await _signalwire_parallel_join_swml(
                    conference_name=conference_name, session_id=session_id,
                    leg_id=leg_id,
                ))
        if session.get("status") not in {"dialing", "connected"}:
            return _swml(build_hangup_swml())

        amd_result = (_extract(payload, dict(query), ("amd_result", "detect_result")) or "unknown").lower()
        if amd_result not in {"human", "machine", "fax", "unknown", "error"}:
            amd_result = "unknown"
        # The rep pressed "Send my voicemail" while this line rang (or during
        # its greeting) and has already moved on.
        queued_by_rep = (
            int(session.get("line_count") or 0) == 1
            and await signalwire_parallel_store.leg_voicemail_requested(session_id=session_id, leg_id=leg_id)
        )
        raw_ms = _extract(payload, dict(query), ("amd_ms", "detect_ms"))
        try:
            amd_ms = max(0, min(int(raw_ms), 120_000))
        except (TypeError, ValueError):
            amd_ms = None

        if amd_result in {"machine", "fax"}:
            await signalwire_parallel_store.update_leg(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, status="machine",
                amd_result=amd_result, amd_ms=amd_ms,
            )
            policy = await signalwire_parallel_store.get_voicemail_policy(
                rep_user_id=str(session["rep_user_id"]),
                entry_id=str(leg["entry_id"]),
                parent_call_sid=str(session["rep_call_id"]),
            )
            if not policy:
                return _swml(build_hangup_swml())
            on_machine = str(policy.get("on_machine") or "hangup")
            single_line = int(session.get("line_count") or 0) == 1
            # Single call: the rep's own choice (else the team default).
            # Parallel line: always, whenever the team action is drop_vm.
            auto_send = queued_by_rep or effective_auto_send(policy, parallel=not single_line)
            beep_heard = _truthy(_extract(payload, dict(query), ("beep",)))
            # Drop VM that does not play automatically on a single call (the
            # rep chose to be asked) passes the mailbox to the rep, who gets
            # the one-tap "Send my voicemail" prompt or can speak live.
            ask_rep = (
                single_line and on_machine == "drop_vm" and amd_result == "machine" and not auto_send
            )
            async def pass_mailbox_to_rep() -> JSONResponse:
                winner = await signalwire_parallel_store.claim_winner(
                    session_id=session_id, leg_id=leg_id,
                    provider_call_id=provider_call_id, amd_ms=amd_ms,
                    amd_result=amd_result,
                )
                if not winner:
                    return _swml(build_hangup_swml())
                # The winning claim transaction already linked the parent to
                # this exact PSTN leg and its independently selected caller ID.
                # A Drop VM mailbox carries a machine_end_* result so the rep's
                # screen offers "Send my voicemail"; pass-to-rep stays as before.
                if amd_result == "fax":
                    call_amd = "fax"
                elif on_machine == "drop_vm":
                    call_amd = "machine_end_beep" if beep_heard else "machine_end_silence"
                else:
                    call_amd = "machine_start"
                await update_signalwire_dialer_amd(parent_call_sid=provider_call_id, amd_result=call_amd)
                return _swml(await _signalwire_parallel_join_swml(
                    conference_name=conference_name, session_id=session_id,
                    leg_id=leg_id,
                ))

            if ((on_machine == "pass_to_rep" and single_line) or ask_rep) and not queued_by_rep:
                return await pass_mailbox_to_rep()

            # A fax line has no mailbox: never play a recording to it.
            if (on_machine == "drop_vm" or queued_by_rep) and auto_send and amd_result == "machine":
                drop = await _load_signalwire_parallel_default_drop(
                    str(session["rep_user_id"]),
                    entry_id=str(leg["entry_id"]),
                    parent_call_sid=str(session["rep_call_id"]),
                )
                # Sign BEFORE claiming: a claim followed by a signing failure
                # used to leave the leg in voicemail_playing forever, which kept
                # the whole batch from settling.
                recording_url = None
                if drop:
                    try:
                        recording_url = await _signed_signalwire_voicemail_url(drop["storage_path"])
                    except Exception as exc:  # noqa: BLE001 - fall back to recording the voicemail
                        logger.error(f"Could not sign SignalWire voicemail for leg {leg_id}: {exc}")
                if drop and recording_url and await signalwire_parallel_store.claim_voicemail_drop(
                    session_id=session_id, leg_id=leg_id,
                    provider_call_id=provider_call_id, drop_id=drop["id"],
                ):
                    backend_endpoint, _ = await get_backend_endpoints()
                    playback_url = _parallel_callback_url(
                        str(backend_endpoint or "").rstrip("/"),
                        "sw-parallel-playback", session_id, leg_id,
                    )
                    call_amd = "machine_end_beep" if beep_heard else "machine_end_silence"
                    await update_signalwire_dialer_amd(
                        parent_call_sid=provider_call_id,
                        amd_result=call_amd,
                        voicemail_started=True,
                    )
                    # The leg owns the message now. Release the rep as soon as
                    # no other line still needs them (a single call: at once).
                    _spawn_background(_finish_signalwire_batch_if_settled(
                        session_id=session_id, session=session,
                    ))
                    from api.services.telephony.dialer.swml import build_signalwire_parallel_voicemail_swml
                    return _swml(build_signalwire_parallel_voicemail_swml(
                        recording_url=recording_url,
                        playback_status_url=playback_url,
                    ))
                if single_line and not queued_by_rep:
                    # Automatic send was wanted but nothing playable exists:
                    # let the rep hear the mailbox and speak instead.
                    return await pass_mailbox_to_rep()

            # No policy, enabled drop, or usable default recording: record the
            # detected voicemail and finish this prospect leg without playback.
            await signalwire_parallel_store.update_leg(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, status="voicemail",
                amd_result=amd_result, amd_ms=amd_ms,
            )
            await update_signalwire_dialer_amd(
                parent_call_sid=provider_call_id,
                amd_result="fax" if amd_result == "fax" else "machine_end_silence",
            )
            await signalwire_parallel_store.record_outcome(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, outcome="voicemail",
            )
            return _swml(build_hangup_swml())

        if queued_by_rep:
            # A person answered after the rep queued a voicemail and left.
            # Never bridge them into an empty room and never play a recording
            # to a person: end the line and leave the lead for the rep.
            await signalwire_parallel_store.update_leg(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, status="completed",
                amd_result=amd_result, amd_ms=amd_ms,
            )
            await update_signalwire_dialer_amd(
                parent_call_sid=provider_call_id,
                amd_result="human" if amd_result == "human" else "unknown",
            )
            await update_dialer_call_status(
                parent_call_sid=provider_call_id, child_call_sid=None, status="completed",
                duration_seconds=None, rep_user_id=session.get("rep_user_id"), provider="signalwire",
            )
            _spawn_background(_finish_signalwire_batch_if_settled(session_id=session_id, session=session))
            return _swml(build_hangup_swml())

        await signalwire_parallel_store.update_leg(
            session_id=session_id, leg_id=leg_id,
            provider_call_id=provider_call_id, status="human",
            amd_result=amd_result, amd_ms=amd_ms,
        )
        winner = await signalwire_parallel_store.claim_winner(
            session_id=session_id, leg_id=leg_id,
            provider_call_id=provider_call_id, amd_ms=amd_ms,
            amd_result=amd_result,
        )
        if not winner:
            # A second human cannot join the rep's winner room: that would put
            # both prospects on the same call. Reserve a teammate and a new
            # conference transactionally, then notify them off the webhook path.
            overflow = await signalwire_parallel_store.route_to_teammate(
                session_id=session_id,
                leg_id=leg_id,
                provider_call_id=provider_call_id,
            )
            if overflow:
                notify_inbound_call(str(overflow["inbound_call_id"]))
                _spawn_background(_expire_signalwire_overflow_offer(provider_call_id))
                await update_dialer_call_status(
                    parent_call_sid=provider_call_id,
                    child_call_sid=None,
                    status="in-progress",
                    duration_seconds=None,
                    rep_user_id=session.get("rep_user_id"),
                    provider="signalwire",
                )
                return _swml(build_signalwire_parallel_overflow_swml(
                    conference_name=str(overflow["conference_name"]),
                    status_callback_url=await _signalwire_overflow_callback_url(str(overflow["conference_name"])),
                ))

            # No teammate was reachable or the configured route is unsupported.
            # Do not leave a second live person listening to silence.
            await signalwire_parallel_store.update_leg(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, status="completed",
                amd_result=amd_result, amd_ms=amd_ms,
            )
            await signalwire_parallel_store.record_outcome(
                session_id=session_id, leg_id=leg_id,
                provider_call_id=provider_call_id, outcome="overflow_unrouted",
            )
            return _swml(build_hangup_swml())
        # Identity, caller ID and status were committed with the winning
        # claim. A second network write cannot invalidate a verified winner.
        # Provider cancellation and history reconciliation must not make the
        # winning human wait for all other PSTN requests to finish.
        _spawn_background(_cancel_signalwire_ringing_siblings(
            session_id=session_id, winner_leg_id=leg_id,
            rep_user_id=str(session.get("rep_user_id") or ""),
        ))
        return _swml(await _signalwire_parallel_join_swml(
            conference_name=conference_name, session_id=session_id,
            leg_id=leg_id,
        ))
    except Exception as exc:  # noqa: BLE001 - provider leg must fail closed
        logger.exception(f"SignalWire parallel decision failed safely: {exc}")
        return _swml(build_hangup_swml())




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
            rep_user_id = await _rep_supabase_id(identity)
            if not rep_user_id or not await is_claimed_inbound_conference(
                conference_name=conference_name, rep_user_id=rep_user_id,
            ):
                logger.warning("SignalWire conference join rejected: no matching inbound claim")
                return _swml(build_hangup_swml())
            rep_call_id = _extract(payload, query, _CALL_ID_KEYS)
            if not rep_call_id or not await bind_inbound_rep_leg(
                conference_name=conference_name, rep_user_id=rep_user_id,
                provider="signalwire", rep_call_id=rep_call_id,
            ):
                logger.warning("SignalWire conference join rejected: unbound/replaced rep leg")
                return _swml(build_hangup_swml())
            logger.info(
                f"sw-dialer-connect joining conference {conference_name!r} for {identity!r}"
            )
            callback_url = (
                await _signalwire_overflow_callback_url(conference_name)
                if conference_name.startswith("sysevo-overflow-") else None
            )
            return _swml(build_conference_join_swml(
                conference_name=conference_name, status_callback_url=callback_url,
            ))

        requested_mode = _extract(payload, query, ("DialMode", "dial_mode"))
        normalized_mode = (requested_mode or "").strip().lower()
        if normalized_mode in {"parallel", "screened_single"}:
            return await _start_signalwire_session(
                request=request, payload=payload, query=query, identity=identity,
                screened_single=normalized_mode == "screened_single",
            )

        lead_number = _extract_lead_number(payload, query)
        if not lead_number:
            return await _hangup_and_record(
                reason="no dialable lead number in the payload or query string",
                identity=identity, payload=payload, query=query,
            )

        caller_id = await _resolve_signalwire_caller_id(identity)
        if not caller_id:
            return await _hangup_and_record(
                reason="no usable caller ID - set SIGNALWIRE_DEFAULT_CALLER_ID",
                identity=identity, payload=payload, query=query,
                lead_number=lead_number,
            )

        # Correlation key for the status/recording callbacks. Preferred from
        # the payload so those callbacks can find this row; a synthetic id
        # keeps the row (and the rep's call history) rather than dropping it
        # when the payload carries no id we recognise.
        call_id = _extract(payload, query, _CALL_ID_KEYS) or f"sw-{uuid.uuid4().hex}"

        rep_user_id = await _rep_supabase_id(identity)
        if rep_user_id:
            block_reason = await phone_block_reason(lead_number)
            if block_reason:
                logger.warning(f"SignalWire outbound call blocked by server suppression check ({block_reason})")
                return _swml(build_hangup_swml())
            allowed, reason, phone_us_state = await calling_hours_result(lead_number, entry_id)
            if not allowed:
                logger.warning(f"SignalWire outbound call blocked by calling-hours check ({reason})")
                return _swml(build_hangup_swml())
            if not await create_dialer_call(
                parent_call_sid=call_id,
                rep_user_id=rep_user_id,
                entry_id=entry_id,
                from_number=caller_id,
                to_number=lead_number,
                provider="signalwire",
                phone_us_state=phone_us_state,
            ):
                logger.error("SignalWire outbound call rejected: call history could not be persisted")
                return _swml(build_hangup_swml())
        elif (identity or "").strip():
            # A supplied but unmappable identity is a failed dialer authorization.
            # Only calls with no rep identity (agent/campaign calls) use this
            # endpoint without dialer history or rep-level compliance checks.
            logger.warning(
                "sw-dialer-connect rejected: caller identity did not resolve to a dialer user"
            )
            return _swml(build_hangup_swml())
        else:
            # No identity at all means no rep: an agent or campaign leg, which has
            # never belonged in dialer_calls and is not a fault.
            logger.debug(
                "sw-dialer-connect: no rep identity on this leg (agent or campaign "
                "call) - connecting it, and not recording it in Recent Calls"
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
        connect_status_webhook = _webhook_url(
            backend_endpoint, "sw-connect-status", call_id
        )

        return _swml(
            build_dialer_swml(
                lead_number=lead_number,
                caller_id=caller_id,
                call_state_webhook=call_state_webhook,
                connect_status_webhook=connect_status_webhook,
                recording_webhook=recording_webhook,
                tap_websocket=_tap_websocket_url(backend_endpoint, call_id),
            )
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: a live leg is waiting
        logger.exception(f"sw-dialer-connect failed, hanging up: {exc}")
        return await _hangup_and_record(
            reason=f"unhandled error: {exc}",
            identity=locals().get("identity"),
            payload=locals().get("payload") or {},
            query=locals().get("query") or {},
        )


async def _hangup_and_record(
    *,
    reason: str,
    identity: str | None,
    payload: dict,
    query: dict,
    lead_number: str | None = None,
    caller_id: str | None = None,
) -> JSONResponse:
    """Hang the leg up, but leave a record that says why.

    A bare ``build_hangup_swml()`` is the most invisible failure this service has. By
    the time this endpoint is reached SignalWire has ALREADY answered the rep's WebRTC
    leg in order to fetch the script, so the softphone has told the rep "connected".
    Returning a hangup therefore reads, on the rep's screen, as a call that connected
    and then dropped by itself -- and because the ``dialer_calls`` row is only created
    further down, after these checks, there was no row, no error and no trace anywhere.
    Reps reported calls "cutting off on their own" and every log looked clean, because
    the only thing that had happened was us telling SignalWire to hang up.

    So each of these now writes the row first, with ``status='failed'``, before the
    hangup goes back. The call still ends -- there is genuinely nothing to connect it
    to -- but it ends visibly: in the rep's Recent Calls, in the disposition counts,
    and on the admin analysis page that already reads this table.
    """
    logger.error(f"sw-dialer-connect hanging up: {reason}")
    try:
        rep_user_id = await _rep_supabase_id(identity) if identity else None
        if rep_user_id:
            call_id = _extract(payload, query, _CALL_ID_KEYS) or f"sw-{uuid.uuid4().hex}"
            await create_dialer_call(
                parent_call_sid=call_id,
                rep_user_id=rep_user_id,
                entry_id=_extract(payload, query, _ENTRY_KEYS) or None,
                from_number=caller_id or "",
                to_number=lead_number or "",
                provider="signalwire",
            )
            await update_dialer_call_status(
                parent_call_sid=call_id,
                child_call_sid=None,
                status="failed",
                duration_seconds=0,
            )
    except Exception as exc:  # noqa: BLE001 - recording must never stop the hangup
        logger.error(f"sw-dialer-connect could not record the hangup: {exc}")
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
    # SWML event logs use camelCase (`noAnswer`), while older callback examples
    # and parts of our own code use snake_case (`no_answer`). Normalize both
    # spellings before mapping so an unanswered call never falls through to
    # `completed`, which the browser reasonably interprets as a conversation.
    reason = re.sub(r"[^a-z]", "", (end_reason or "").strip().lower())

    if normalised == "answered":
        return "in-progress"
    if normalised == "ringing":
        return "ringing"
    if normalised == "created":
        return "initiated"
    if normalised == "ended":
        return {
            "noanswer": "no-answer",
            "busy": "busy",
            "decline": "canceled",
            "declined": "canceled",
            "cancel": "canceled",
            "cancelled": "canceled",
            "canceled": "canceled",
            "abandoned": "canceled",
            "error": "failed",
            "notfound": "failed",
            "maxduration": "completed",
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

        if not _secret_ok("sw-call-status", request, allow_per_call_signature=True):
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
        end_source = _extract(payload, query, _END_SOURCE_KEYS)
        ended_by = _ended_by(
            state or "",
            end_source,
            _extract(payload, query, _END_REASON_KEYS) or "",
        )

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

        status = _map_call_state(state, end_reason)
        # SignalWire's observed `connect` rejection (HTTP 400: "No valid devices")
        # ends the SWML leg as hangup with end_source=none. That is not a human
        # completing a conversation. Mapping it to completed made the dialer treat
        # failed outbound attempts like successful calls with a missing recording.
        if (
            (state or "").strip().lower() == "ended"
            and re.sub(r"[^a-z]", "", (end_reason or "").strip().lower()) == "hangup"
            and (end_source or "").strip().lower() == "none"
        ):
            status = "failed"
            logger.warning(
                f"SignalWire call {call_id} ended without a connected peer; recording as failed"
            )

        await update_dialer_call_status(
            parent_call_sid=call_id,
            child_call_sid=None,
            status=status,
            duration_seconds=duration,
            ended_by=ended_by,
        )
        return JSONResponse(content={"ok": True})
    except Exception as exc:  # noqa: BLE001 - a status callback must never 500
        logger.exception(f"sw-call-status failed: {exc}")
        return JSONResponse(content={"ok": True})


@router.post("/sw-connect-status", include_in_schema=False)
async def handle_sw_connect_status(request: Request):
    """Persist SignalWire connect-operation results, including failed_reason.

    This is separate from the call-state callback: a `failed` connect event carries
    actionable routing outcomes such as `no_answer`, `busy`, and `not_found`. Keeping
    it separate avoids letting a late `disconnected` event overwrite the final B-leg
    status and duration recorded by /sw-call-status.
    """
    try:
        payload = await _read_payload(request)
        _log_payload("sw-connect-status", request, payload)
        if not _secret_ok("sw-connect-status", request, allow_per_call_signature=True):
            logger.warning("sw-connect-status rejected: bad or missing ?k= secret")
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

        query = dict(request.query_params)
        call_id = (query.get("call_id") or "").strip() or _extract(
            payload, query, _CALL_ID_KEYS
        )
        state = _extract(payload, query, ("connect_state", "connectState"))
        failed_reason = _extract(payload, query, ("failed_reason", "failedReason"))
        if not call_id or not state:
            logger.warning(
                f"sw-connect-status missing call id ({call_id!r}) or connect state ({state!r})"
            )
            return JSONResponse(content={"ok": True})

        state = state.strip().lower()
        if state == "connected":
            status = "in-progress"
        elif state == "failed":
            status = _map_call_state("ended", failed_reason)
            if status == "completed":
                status = "failed"
        else:
            return JSONResponse(content={"ok": True})

        await update_dialer_call_status(
            parent_call_sid=call_id,
            child_call_sid=None,
            status=status,
            duration_seconds=None,
        )
        return JSONResponse(content={"ok": True})
    except Exception as exc:  # noqa: BLE001 - provider callbacks must never 500
        logger.exception(f"sw-connect-status failed: {exc}")
        return JSONResponse(content={"ok": True})


@router.post("/sw-recording", include_in_schema=False)
async def handle_sw_recording(request: Request):
    """Recording callback. Same 401-not-SWML reasoning as sw-call-status."""
    try:
        payload = await _read_payload(request)
        _log_payload("sw-recording", request, payload)

        if not _secret_ok("sw-recording", request, allow_per_call_signature=True):
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
            logger.warning(f"sw-inbound: nobody available for {to_number} - caller {from_number}")
            return _swml(await _no_answer_swml(caller_number=from_number, rang_number=to_number))

        recording_webhook = ""
        try:
            backend_endpoint, _ = await get_backend_endpoints()
        except Exception as exc:  # noqa: BLE001 - never break call setup
            logger.error(f"sw-inbound could not resolve backend endpoint: {exc}")
            backend_endpoint = ""
        backend_endpoint = str(backend_endpoint or "")
        if backend_endpoint.startswith(("http://", "https://")):
            recording_webhook = _webhook_url(
                backend_endpoint, "sw-recording", call_id
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
            # RING FIRST: the reps' dialers ring; nobody answering in time moves the caller
            # to the inbound agent instead of leaving them on hold until they give up. Set on
            # the dialer's Agents page (ring-first seconds, the agent, and per-number
            # hand-over); off when no timer is set or the number is switched off.
            agent = await current_inbound_agent(number=to_number)
            wait = agent.ring_first_seconds
            if wait and agent.number and await number_hands_to_agent(to_number):
                agent_url = _reroute_url(backend_endpoint, call_id, "agent")
                if agent_url:
                    sam_handoff.spawn(sam_handoff.hand_to_sam_if_unanswered(call_id, wait, agent_url))
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
        return _swml(await _no_answer_swml(caller_number=from_number, rang_number=to_number))
    except Exception as exc:  # noqa: BLE001 - a live caller is waiting
        logger.exception(f"sw-inbound failed, hanging up: {exc}")
        return _swml(build_hangup_swml())


@router.post("/sw-inbound-reroute", include_in_schema=False)
async def handle_sw_inbound_reroute(request: Request):
    """SWML for a live inbound call being MOVED (SignalWire calling.transfer points here).

    mode=agent       nobody answered: over to Sam INBOUND
    mode=agent_back  a rep missed Sam's transfer: back to Sam, told why
    mode=hold        Sam is handing the caller to the rep: into the room the rep's
                     dialer joins, exactly like a fresh inbound call
    """
    try:
        payload = await _read_payload(request)
        _log_payload("sw-inbound-reroute", request, payload)
        if not _secret_ok(
            "sw-inbound-reroute", request, allow_per_call_signature=True
        ):
            return _swml(build_hangup_swml())
        call_id = (request.query_params.get("call_id") or "").strip()
        mode = (request.query_params.get("mode") or "").strip()
        agent = await _inbound_agent_number(call_id=call_id)
        if mode in ("agent", "agent_back") and agent:
            return _swml(
                build_agent_overflow_swml(
                    agent_number=agent,
                    prelude=(
                        "They're tied up on another call right now, so I'll put you back through."
                        if mode == "agent_back"
                        else ""
                    ),
                )
            )
        if mode == "hold" and call_id:
            try:
                backend_endpoint, _ = await get_backend_endpoints()
            except Exception:  # noqa: BLE001
                backend_endpoint = ""
            return _swml(
                build_inbound_hold_swml(
                    conference_name=f"inbound-{call_id}",
                    recording_webhook=_webhook_url(str(backend_endpoint or ""), "sw-recording", call_id),
                    greeting="Connecting you now.",
                    tap_websocket=_tap_websocket_url(str(backend_endpoint or ""), call_id),
                )
            )
        logger.error(f"sw-inbound-reroute: nothing to do for mode={mode!r} call={call_id!r}")
        return _swml(build_no_agents_swml())
    except Exception as exc:  # noqa: BLE001 - a live caller is on this call
        logger.exception(f"sw-inbound-reroute failed: {exc}")
        return _swml(build_no_agents_swml())


@router.get("/inbound-agent-status", include_in_schema=False)
async def handle_inbound_agent_status(user: UserModel = Depends(get_user)):
    """What the server is ACTUALLY doing with inbound calls nobody answers.

    The Agents page shows this rather than its own saved values: a setting that is saved
    but not acted on is exactly the kind of thing that reads as working. Read fresh, not
    from the call-path cache, so a change shows up the moment it is made.
    """
    agent = await current_inbound_agent(fresh=True,user_id=str(user.provider_id))
    return JSONResponse(
        {
            "live": bool(agent.number),
            "source": agent.source,
            "number": agent.number,
            "workflow_id": agent.workflow_id,
            "ring_first_seconds": agent.ring_first_seconds,
            "problem": agent.problem,
        }
    )


@router.post("/sam-transfer-to-rep", include_in_schema=False)
async def handle_sam_transfer_to_rep(request: Request):
    """Sam INBOUND's transfer_to_rep tool: ring the rep who owns the number the caller rang.

    The tool sends {{workflow_run_id}} and {{caller_number}} from Sam's own call, and the
    request is honoured only for a live Sam INBOUND run of that caller (see
    transfer_request_allowed). The reply is what Sam says next: "ringing_rep" (the caller
    is about to be moved, say nothing more) or "unavailable" (book a meeting instead).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    caller = str(body.get("caller_number") or "")
    try:
        run_id = int(str(body.get("workflow_run_id") or "0"))
    except ValueError:
        run_id = 0
    run = await db_client.get_workflow_run(run_id) if run_id else None
    ok, reason = transfer_request_allowed(
        run_workflow_id=getattr(run, "workflow_id", None),
        run_is_completed=getattr(run, "is_completed", None),
        run_caller_number=((getattr(run, "initial_context", None) or {}).get("caller_number")),
        claimed_caller_number=caller,
        sam_workflow_id=await _sam_inbound_workflow_id(),
    )
    if not ok:
        logger.warning(f"sam-transfer-to-rep refused (run {run_id}): {reason}")
        return JSONResponse(status_code=403, content={"status": "refused", "reason": reason})

    unavailable = {
        "status": "unavailable",
        "say": "The team is tied up on calls right now. Offer to book a time on the calendar, or take their name, reason and best time for a callback.",
    }
    call = await sam_handoff.find_live_call_for(caller)
    if not call:
        # Rang Sam's own number (not a rep's), or the inbound record is gone: nobody to ring.
        return JSONResponse(content={**unavailable, "reason": "no rep line behind this call"})
    call_id = call["provider_call_id"]
    plan = await resolve_inbound_plan(call.get("to_number") or "")
    targets = [str(p["user_id"]) for p in plan if p.get("user_id")]
    if not targets:
        return JSONResponse(content={**unavailable, "reason": "the rep is on another call or offline"})

    try:
        backend_endpoint, _ = await get_backend_endpoints()
    except Exception:  # noqa: BLE001
        backend_endpoint = ""
    hold_url = _reroute_url(str(backend_endpoint or ""), call_id, "hold")
    back_url = _reroute_url(str(backend_endpoint or ""), call_id, "agent_back")
    row = await sam_handoff.ring_again(call_id, targets)
    if not row or not hold_url:
        return JSONResponse(content={**unavailable, "reason": "could not reach the rep's dialer"})
    if not await sam_handoff.move_call(call_id, hold_url):
        # Leave everything as it was: stop ringing, Sam keeps the caller.
        await sam_handoff.mark_missed_if_ringing(call_id)
        return JSONResponse(content={**unavailable, "reason": "could not move the call"})
    sam_handoff.spawn(sam_handoff.back_to_sam_if_rep_misses(call_id, back_url))
    logger.info(f"sam-transfer-to-rep: {call_id} moved to the rep's dialer ({len(targets)} rung, {REP_ANSWER_SECONDS}s)")
    return JSONResponse(content={"status": "ringing_rep", "say": "Say only: 'Putting you through now.' The call is being moved to the rep."})


@router.post("/sam-knowledge", include_in_schema=False)
async def handle_sam_knowledge(request: Request):
    """Sam INBOUND's sysevo_knowledge tool: the team's checked answer to a caller's question.

    Honoured on the same terms as transfer_to_rep — a live Sam INBOUND run and that call's
    caller number, both filled in by Dograh from Sam's own call — so the documents are not
    readable by anyone who finds the URL. The reply tells Sam what to say; see answer_for.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        run_id = int(str(body.get("workflow_run_id") or "0"))
    except ValueError:
        run_id = 0
    run = await db_client.get_workflow_run(run_id) if run_id else None
    ok, reason = knowledge_request_allowed(
        run_workflow_id=getattr(run, "workflow_id", None),
        run_is_completed=getattr(run, "is_completed", None),
        run_mode=getattr(run, "mode", None),
        run_caller_number=((getattr(run, "initial_context", None) or {}).get("caller_number")),
        claimed_caller_number=str(body.get("caller_number") or ""),
        sam_workflow_id=await _sam_inbound_workflow_id(),
    )
    if not ok:
        logger.warning(f"sam-knowledge refused (run {run_id}): {reason}")
        return JSONResponse(status_code=403, content={"status": "refused", "reason": reason})

    question = str(body.get("question") or "").strip()[:500]
    reply = answer_for(await sam_knowledge.load_entries(), question)
    logger.info(f"sam-knowledge: run {run_id} asked {question!r} -> {reply['status']}")
    return JSONResponse(content=reply)


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

    # The per-call token. Checked AFTER call_id, because the token is derived from it and
    # there is nothing to compare against until we have one.
    #
    # Enforced exactly when it is issuable: _tap_token() is the same function that built
    # the URL, reading the same environment, so "we expect a token" and "the URL carries
    # a token" are the same condition by construction. That is the whole point -- the
    # outage this replaces came from turning on a check whose counterpart lived somewhere
    # else entirely and could not be turned on with it.
    expected_token = _tap_token(call_id)
    if expected_token and not hmac.compare_digest(
        str(websocket.query_params.get("t", "")), expected_token
    ):
        # 4401 like the shared-secret path: refused before the handshake is accepted, so
        # an unauthenticated caller is not even told the call id was valid.
        await websocket.close(code=4401, reason="bad token")
        return
    if not expected_token:
        # Only reachable with no SignalWire credentials at all, which is a box that
        # cannot place calls. Loud, because it is the state this fix exists to end.
        logger.warning(
            "No SignalWire secret available - /sw-tap is UNAUTHENTICATED for "
            f"call {call_id}."
        )

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

        # Mixed mono tap: nobody can be named as the speaker, so speaker stays None.
        async with LiveTranscriber(
            call_id, on_caption, speaker=None, source="signalwire_tap"
        ) as transcriber:
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
