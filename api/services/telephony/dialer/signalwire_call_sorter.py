"""SignalWire side of the call sorter: an SWML `tap` of the prospect's audio only.

SWML's `tap` forks a call's media to a WebSocket and leaves the call alone -- the
same verb /sw-tap already uses for live listening (swml._tap_step). Its `direction`
is from the point of view of the leg running the SWML
(https://signalwire.com/docs/swml/reference/calling/tap):
    speak   what that leg's party says
    listen  what that leg's party hears
    both    mixed

So:
* Screened / parallel prospect legs (build_signalwire_parallel_leg_swml) run SWML
  ON THE PROSPECT'S LEG: tap `speak` = the prospect only, and the tap starts when
  that SWML is fetched, i.e. at answer -- before detect_machine. This is the
  recommended placement.
* The rep-leg bridge (build_dialer_swml) runs on the REP's leg: tap `listen` = the
  far end. It also carries early media (carrier ringback / "not in service"), so
  stages from it can arrive BEFORE answer; only use it with that in mind.

The tap is `control_id`-named so a later SWML step can `stop_tap` it; in practice
the sorter closes the socket when it has decided, which ends the fork.

Auth: the URL is ours (built per call, never a dashboard URL), so it can carry a
signed query: ?call_ref=&leg=&t=<expiry.hmac>, checked BEFORE the handshake is
accepted. Keyed like /sw-tap (SIGNALWIRE_WEBHOOK_KEY, else SIGNALWIRE_API_TOKEN --
both already on any box that places SignalWire calls) but under the sorter's own
token domain, so a /sw-tap token cannot open this socket.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from contextlib import suppress
from typing import Callable
from urllib.parse import urlencode

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

TAP_PATH = "/api/v1/telephony/sw-call-sorter"
TAP_CONTROL_ID = "call-sorter"


def _secret() -> str:
    return (
        os.environ.get("SIGNALWIRE_WEBHOOK_KEY")
        or os.environ.get("SIGNALWIRE_API_TOKEN")
        or ""
    ).strip()


def sorter_tap_url(
    *, backend_endpoint: str, call_ref: str, leg_id: str, now: int | None = None
) -> str:
    """wss:// address for one call's sorter tap, or "" when it cannot be built."""
    if not call_sorter_enabled():
        return ""
    base = wss_url(backend_endpoint, TAP_PATH)
    token = issue_sorter_token(secret=_secret(), call_ref=call_ref, leg_id=leg_id, now=now)
    if not base or not token:
        return ""
    return base + "?" + urlencode({"call_ref": call_ref, "leg": leg_id, "t": token})


def prospect_tap_step(tap_url: str, *, on_prospect_leg: bool = True) -> dict | None:
    """The SWML step forking the prospect's audio; None when there is no URL."""
    if not tap_url:
        return None
    return {
        "tap": {
            "uri": tap_url,
            "direction": "speak" if on_prospect_leg else "listen",
            "codec": "PCMU",
            "control_id": TAP_CONTROL_ID,
        }
    }


def stop_tap_step() -> dict:
    return {"stop_tap": {"control_id": TAP_CONTROL_ID}}


def with_prospect_tap(swml: dict, tap_step: dict | None, *, before: str | None = None) -> dict:
    """A copy of `swml` with `tap_step` inserted into sections.main.

    Inserted before the first step whose verb is `before` (e.g. "connect" for the
    rep-leg bridge, so the tap is running when the far end answers), else first.
    Returns `swml` unchanged when there is no step -- the sorter is optional.
    """
    if not tap_step:
        return swml
    sections = dict((swml or {}).get("sections") or {})
    main = list(sections.get("main") or [])
    index = 0
    if before:
        index = next(
            (i for i, step in enumerate(main) if isinstance(step, dict) and before in step),
            len(main),
        )
    main.insert(index, tap_step)
    sections["main"] = main
    return {**swml, "sections": sections}


def decode_tap_message(message: dict) -> bytes:
    """Audio from one websocket message: binary frames as-is; also accepts a
    Twilio-style JSON media frame, should the tap ever send JSON."""
    data = message.get("bytes")
    if data:
        return bytes(data)
    text = message.get("text")
    if not text:
        return b""
    try:
        payload = (json.loads(text).get("media") or {}).get("payload")
        return base64.b64decode(payload, validate=True) if isinstance(payload, str) else b""
    except (TypeError, ValueError, AttributeError, binascii.Error):
        return b""


async def serve_signalwire_sorter(
    websocket,
    *,
    sorter_factory: Callable[..., CallSorter] = CallSorter,
    transcriber_factory=LiveTranscriber,
) -> CallSorter | None:
    """Authenticate (before accepting), then sort one tap's audio."""
    call_ref = (websocket.query_params.get("call_ref") or "").strip()
    leg_id = (websocket.query_params.get("leg") or "").strip()
    token = (websocket.query_params.get("t") or "").strip()
    if not verify_sorter_token(secret=_secret(), call_ref=call_ref, leg_id=leg_id, token=token):
        # Refused before the handshake: an unauthenticated caller learns nothing.
        await websocket.close(code=4401, reason="not permitted")
        return None
    await websocket.accept()
    sorter = sorter_factory(call_ref, provider="signalwire")

    async def frames():
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            audio = decode_tap_message(message)
            if audio:
                yield audio

    await run_call_sorter(sorter, frames(), transcriber_factory=transcriber_factory)
    with suppress(Exception):
        await websocket.close(code=1000, reason=sorter.stop_reason or "done")
    logger.info(f"signalwire call sorter finished for {call_ref}: {sorter.stages}")
    return sorter


__all__ = [
    "TAP_CONTROL_ID",
    "TAP_PATH",
    "decode_tap_message",
    "prospect_tap_step",
    "serve_signalwire_sorter",
    "sorter_tap_url",
    "stop_tap_step",
    "with_prospect_tap",
]
