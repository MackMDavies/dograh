"""Twilio side of the call sorter: fork the PROSPECT leg's audio, one way, to us.

The dialer's prospect leg is NOT a <Dial> child: dial_lead_into_conference creates it
with the REST Calls API (url=/dialer-conference-join), so it is a call of its own and
its `inbound_track` is exactly what the prospect (or their mailbox) sends. A
unidirectional Media Stream on it is a fork -- the conference audio is untouched, and
closing our socket ends the fork without touching the call.

Two ways to start it, because of synchronous AMD:

* REST (preferred) -- `start_prospect_stream(client, ...)` from the lead leg's
  `answered` status callback. Synchronous AMD (MachineDetection=DetectMessageEnd)
  holds the TwiML fetch until the greeting has ENDED, so TwiML returned by
  /dialer-conference-join would start listening after the part worth hearing.
  The Stream resource works on any in-progress call.
* TwiML -- `start_stream_twiml(...)` prepended to the join TwiML. Right for legs
  placed without AMD (or with AsyncAmd, where the TwiML runs at answer), and still
  useful after a "human" AMD verdict: screeners and IVRs are usually called human.

Twilio's docs on both: https://www.twilio.com/docs/voice/api/stream-resource and
https://www.twilio.com/docs/voice/twiml/stream. AsyncAmd itself consumes one of the
four forked streams a call may have (answering-machine-detection docs), which leaves
room for this one.

Authentication: Media Streams cannot carry a query string, so the credential rides
as <Parameter> values (REST: Parameter1/2), revealed in the `start` frame. The token
binds the call_ref AND the prospect leg's CallSid; the socket checks `start.callSid`
against it, so a leaked token cannot be replayed from another call. Keyed by the
dialer's Twilio auth token, the same secret the caption stream uses -- but under
its own domain string, so neither token opens the other's socket.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from contextlib import suppress
from typing import Awaitable, Callable
from xml.sax.saxutils import quoteattr

from loguru import logger

from api.services.telephony.dialer.call_sorter import (
    CallSorter,
    call_sorter_enabled,
    issue_sorter_token,
    run_call_sorter,
    verify_sorter_token,
    wss_url,
)
from api.services.telephony.dialer.live_transcribe import LiveTranscriber

STREAM_PATH = "/api/v1/telephony/dialer-call-sorter"
_START_TIMEOUT_SECONDS = 10.0


def stream_name(leg_call_sid: str) -> str:
    """Our name for the fork, so it can be stopped by name (TwiML or REST)."""
    return f"sorter-{leg_call_sid}"


def _stream_target(
    *, backend_endpoint: str, call_ref: str, leg_call_sid: str, secret: str, now: int | None
) -> tuple[str, str]:
    url = wss_url(backend_endpoint, STREAM_PATH)
    token = issue_sorter_token(secret=secret, call_ref=call_ref, leg_id=leg_call_sid, now=now)
    return url, token


def start_stream_twiml(
    *, backend_endpoint: str, call_ref: str, leg_call_sid: str, secret: str,
    now: int | None = None,
) -> str:
    """`<Start><Stream track="inbound_track">` for the prospect leg, or "".

    "" whenever the sorter is off or anything is missing, so it can be concatenated
    into existing TwiML unconditionally.
    """
    if not call_sorter_enabled():
        return ""
    url, token = _stream_target(
        backend_endpoint=backend_endpoint, call_ref=call_ref,
        leg_call_sid=leg_call_sid, secret=secret, now=now,
    )
    if not url or not token:
        return ""
    return (
        "<Start>"
        f"<Stream name={quoteattr(stream_name(leg_call_sid))} url={quoteattr(url)}"
        ' track="inbound_track">'
        f'<Parameter name="call_ref" value={quoteattr(call_ref)}/>'
        f'<Parameter name="token" value={quoteattr(token)}/>'
        "</Stream>"
        "</Start>"
    )


def stop_stream_twiml(*, leg_call_sid: str) -> str:
    """`<Stop><Stream name=.../></Stop>` -- for TwiML that takes over the prospect
    leg after the sorter is no longer wanted (e.g. playing the voicemail)."""
    if not leg_call_sid:
        return ""
    return f"<Stop><Stream name={quoteattr(stream_name(leg_call_sid))}/></Stop>"


async def start_prospect_stream(
    client, *, backend_endpoint: str, call_ref: str, leg_call_sid: str, secret: str,
    now: int | None = None, force: bool = False,
) -> str | None:
    """Start the fork on an in-progress prospect leg via the Stream resource.

    `client` is a twilio.rest.Client (dialer_conference._twilio_client()). Returns
    the Stream SID, or None when off/misconfigured/refused. Never raises.
    `force` starts it for a beep-trial rep while the sorter is otherwise off.
    """
    if client is None or not (force or call_sorter_enabled()):
        return None
    url, token = _stream_target(
        backend_endpoint=backend_endpoint, call_ref=call_ref,
        leg_call_sid=leg_call_sid, secret=secret, now=now,
    )
    if not url or not token:
        return None
    try:
        stream = await asyncio.to_thread(
            client.calls(leg_call_sid).streams.create,
            url=url,
            name=stream_name(leg_call_sid),
            track="inbound_track",
            parameter1_name="call_ref",
            parameter1_value=call_ref,
            parameter2_name="token",
            parameter2_value=token,
        )
        return getattr(stream, "sid", None) or "started"
    except Exception as exc:  # noqa: BLE001 - listening is optional; the call is not
        logger.warning(f"call sorter stream not started on {leg_call_sid}: {exc}")
        return None


async def stop_prospect_stream(client, *, leg_call_sid: str) -> bool:
    """Stop the fork by name. Closing our socket also stops it; this is for a
    caller that wants it gone before the socket notices. Never raises."""
    if client is None or not leg_call_sid:
        return False
    try:
        await asyncio.to_thread(
            client.calls(leg_call_sid).streams(stream_name(leg_call_sid)).update,
            status="stopped",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info(f"call sorter stream stop on {leg_call_sid}: {exc}")
        return False


def decode_inbound_media(message: dict) -> bytes:
    """mu-law bytes of one `media` frame from the prospect, b"" otherwise."""
    media = message.get("media") or {}
    if str(media.get("track") or "inbound").lower() != "inbound":
        return b""
    payload = media.get("payload")
    if not isinstance(payload, str) or not payload:
        return b""
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return b""


async def _read_start(websocket) -> dict | None:
    while True:
        try:
            message = json.loads(await websocket.receive_text())
        except (TypeError, ValueError):
            continue
        event = message.get("event")
        if event == "start":
            return message.get("start") or {}
        if event == "stop":
            return None


async def serve_twilio_sorter(
    websocket,
    resolve_secret: Callable[[], Awaitable[str | None]],
    *,
    sorter_factory: Callable[..., CallSorter] = CallSorter,
    transcriber_factory=LiveTranscriber,
) -> CallSorter | None:
    """Run one prospect-leg Media Stream through the sorter.

    Twilio only reveals the custom parameters in `start`, so the socket is accepted
    first and closed with 4401 if the token does not verify. Returns the sorter (for
    tests/logging) or None when the stream was refused.
    """
    await websocket.accept()
    try:
        start = await asyncio.wait_for(_read_start(websocket), timeout=_START_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeout, disconnect, malformed
        start = None
    if not start:
        with suppress(Exception):
            await websocket.close(code=4400, reason="no start")
        return None

    params = start.get("customParameters") or {}
    call_ref = str(params.get("call_ref") or "").strip()
    token = str(params.get("token") or "").strip()
    leg_call_sid = str(start.get("callSid") or "").strip()
    secret = ""
    with suppress(Exception):
        secret = (await resolve_secret()) or ""
    if not verify_sorter_token(secret=secret, call_ref=call_ref, leg_id=leg_call_sid, token=token):
        logger.warning("Rejected an unauthenticated call-sorter stream")
        with suppress(Exception):
            await websocket.close(code=4401, reason="not permitted")
        return None

    sorter = sorter_factory(call_ref, provider="twilio")
    # Phases 2 + 3: the verified prospect leg the handling agent may work.
    with suppress(Exception):
        sorter.leg_id = leg_call_sid

    async def frames():
        while True:
            try:
                message = json.loads(await websocket.receive_text())
            except (TypeError, ValueError):
                continue
            event = message.get("event")
            if event == "stop":
                return
            if event == "media":
                audio = decode_inbound_media(message)
                if audio:
                    yield audio

    await run_call_sorter(sorter, frames(), transcriber_factory=transcriber_factory)
    # Closing the socket is what ends a unidirectional fork; the call carries on.
    with suppress(Exception):
        await websocket.close(code=1000, reason=sorter.stop_reason or "done")
    return sorter


__all__ = [
    "STREAM_PATH",
    "decode_inbound_media",
    "serve_twilio_sorter",
    "start_prospect_stream",
    "start_stream_twiml",
    "stop_prospect_stream",
    "stop_stream_twiml",
    "stream_name",
]
