"""Twilio side of call handling (Dialer Phases 2 + 3): work the prospect's leg alone.

DTMF SPIKE (2026-10-20, from Twilio's docs):
* ``<Play digits="...">`` "lets you play DTMF tones during a call"; valid characters
  0-9, A-D, *, #, ``w`` (0.5 s pause) and ``W`` (1 s)
  -- https://www.twilio.com/docs/voice/twiml/play (digits attribute).
* A bidirectional Media Stream can NOT send DTMF: the server may only send
  ``media``, ``mark`` and ``clear`` -- https://www.twilio.com/docs/voice/media-streams/websocket-messages.
* So the documented route is the Call resource update with new TwiML
  (``calls(sid).update(twiml=...)``), which is how this module sends every key and
  every answer. The sorter's fork is a unidirectional Stream on the same leg, stopped
  only by ``<Stop><Stream>`` / the Stream resource (https://www.twilio.com/docs/voice/twiml/stream,
  https://www.twilio.com/docs/voice/api/stream-resource); Phase 1 already relies on it
  outliving a TwiML update (its voicemail TwiML stops the fork by name).
  If the fork is ever lost the session hands the person to the rep (fail-safe).

TOPOLOGY. The prospect leg is a REST-originated call that joins ``call-<parent SID>``
with endConferenceOnExit="true". Taking it out with a TwiML update would end the rep's
conference, so isolate() first sets the participant's EndConferenceOnExit to false
(Participant update, https://www.twilio.com/docs/voice/api/conference-participant-resource),
then gives the leg its own TwiML. The rep stays alone in the room (silence).

Every TwiML document the leg holds ends in a bounded Pause and a Redirect to the
leg's own conference-join URL with ``handled=timeout``: if this worker dies mid-call,
the person is handed to the rep, never parked or dropped.

HAND-BACK is one update to that join URL (``handled=person`` / ``handled=machine``): the
join handler then applies exactly the rules a fresh answer gets (the parallel winner
claim, the extra-answer chain when the rep is busy, Phase 1 voicemail rules for a
mailbox). Replacing the leg's TwiML stops the agent's audio before the Dial runs, so
the agent always leaves before the rep joins.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from api.services.telephony.providers.twilio.dialer_conference import _twilio_client, cancel_call
from api.utils.common import get_backend_endpoints

_SID = re.compile(r"^CA[0-9a-f]{32}$")
_DIGITS = re.compile(r"^[0-9A-D*#wW]{1,40}$")
_XML = '<?xml version="1.0" encoding="UTF-8"?>'
# How long a held leg waits with nothing to do before the fail-safe hand-back.
HOLD_SAFETY_MARGIN_SECONDS = 30


def join_url(*, backend: str, conference_name: str, parent_call_sid: str,
             parallel_leg_id: str = "", parallel_entry_id: str = "", handled: str = "person") -> str:
    """The leg's conference-join URL (same fields voice-connect signs), plus ``handled``."""
    root = backend.rstrip("/") + "/api/v1/telephony"
    fields = {
        "conference_name": conference_name, "muted": "false", "end_on_exit": "true",
        "start_on_enter": "true", "events_url": f"{root}/dialer-conference-events",
        "recording_url": f"{root}/dialer-recording-callback", "parent_call_sid": parent_call_sid,
    }
    if parallel_leg_id and parallel_entry_id:
        fields.update({"parallel_leg_id": parallel_leg_id, "parallel_entry_id": parallel_entry_id})
    fields["handled"] = handled
    return f"{root}/dialer-conference-join?" + urlencode(fields)


def held_twiml(body: str, *, hold_seconds: int, fallback_url: str) -> str:
    """``body`` (a Play / Say / digits), then silence, then the fail-safe hand-back."""
    hold = max(10, min(240, int(hold_seconds)))
    return (f"{_XML}<Response>{body}<Pause length=\"{hold}\"/>"
            f"<Redirect method=\"POST\">{escape(fallback_url)}</Redirect></Response>")


def play_body(*, url: str | None = None, text: str | None = None, say_voice: str | None = None) -> str:
    if url:
        return f"<Play>{escape(url)}</Play>"
    voice = f" voice={quoteattr(say_voice)}" if say_voice else ""
    return f"<Say{voice}>{escape(text or '')}</Say>"


def digits_body(digits: str) -> str:
    if not _DIGITS.fullmatch(digits or ""):
        raise ValueError("invalid DTMF digits")
    # A short lead-in so the first tone is not clipped by the leg's media switch.
    return f"<Play digits={quoteattr('w' + digits)}/>"


class TwilioHandlingLeg:
    provider = "twilio"

    def __init__(self, *, call_key: str, leg_sid: str, parent_call_sid: str, conference_name: str,
                 backend: str, hold_seconds: int, parallel_leg_id: str = "",
                 parallel_entry_id: str = "", client=None):
        self.call_key = call_key
        self.leg_sid = leg_sid
        self.parent_call_sid = parent_call_sid
        self.conference_name = conference_name
        self.backend = backend
        self.hold_seconds = hold_seconds
        self.parallel_leg_id = parallel_leg_id
        self.parallel_entry_id = parallel_entry_id
        self._client = client
        self._stopped = False

    @classmethod
    async def create(cls, *, call_key: str, leg_sid: str, context: dict[str, Any]) -> "TwilioHandlingLeg | None":
        parent = str(context.get("parent_call_sid") or "")
        if not _SID.fullmatch(leg_sid or "") or not _SID.fullmatch(parent):
            return None
        backend, _ = await get_backend_endpoints()
        backend = str(backend or "")
        if not backend.startswith("https://"):
            return None
        settings = context.get("settings") or {}
        hold = int(max(settings.get("agent_max_hold_seconds") or 60, settings.get("ivr_max_seconds") or 60))
        leg_id = str(context.get("leg_id") or "") if leg_sid != parent and context.get("leg_id") else ""
        entry_id = str(context.get("entry_id") or "") if leg_id else ""
        return cls(
            call_key=call_key, leg_sid=leg_sid, parent_call_sid=parent,
            conference_name=str(context.get("conference_name") or f"call-{parent}"),
            backend=backend, hold_seconds=hold + HOLD_SAFETY_MARGIN_SECONDS,
            parallel_leg_id=leg_id, parallel_entry_id=entry_id,
        )

    def _join(self, handled: str) -> str:
        return join_url(backend=self.backend, conference_name=self.conference_name,
                        parent_call_sid=self.parent_call_sid, parallel_leg_id=self.parallel_leg_id,
                        parallel_entry_id=self.parallel_entry_id, handled=handled)

    async def _client_or_none(self):
        if self._client is None:
            self._client = await _twilio_client()
        return self._client

    async def _update(self, **params) -> bool:
        client = await self._client_or_none()
        if client is None:
            return False
        try:
            await asyncio.to_thread(client.calls(self.leg_sid).update, **params)
            return True
        except Exception as exc:  # noqa: BLE001 - the call may have ended
            logger.info(f"call handling: Twilio update of {self.leg_sid} failed: {type(exc).__name__}")
            return False

    async def _held(self, body: str) -> bool:
        return await self._update(twiml=held_twiml(body, hold_seconds=self.hold_seconds,
                                                   fallback_url=self._join("timeout")))

    async def isolate(self) -> bool:
        """Keep the rep's room alive, then give the leg its own (silent) TwiML."""
        client = await self._client_or_none()
        if client is None:
            return False
        try:
            conferences = await asyncio.to_thread(
                client.conferences.list, friendly_name=self.conference_name, status="in-progress", limit=1,
            )
            if conferences:
                await asyncio.to_thread(
                    client.conferences(conferences[0].sid).participants(self.leg_sid).update,
                    end_conference_on_exit=False,
                )
        except Exception as exc:  # noqa: BLE001 - not in the room yet (still in AMD): fine
            logger.info(f"call handling: participant flag not changed for {self.leg_sid}: {type(exc).__name__}")
        return await self._held("")

    async def play(self, *, url: str | None = None, text: str | None = None,
                   say_voice: str | None = None) -> bool:
        if self._stopped or not (url or text):
            return False
        return await self._held(play_body(url=url, text=text, say_voice=say_voice))

    async def send_digits(self, digits: str) -> bool:
        if self._stopped:
            return False
        try:
            body = digits_body(digits)
        except ValueError:
            return False
        return await self._held(body)

    async def stop_agent(self) -> None:
        # No API call: the hand-back's TwiML replaces whatever the agent was playing,
        # and nothing further is sent once stopped.
        self._stopped = True

    async def hand_back(self, *, result: str = "person") -> bool:
        if result not in {"person", "machine", "timeout"}:
            result = "person"
        self._stopped = True
        return await self._update(url=self._join(result), method="POST")

    async def end(self) -> bool:
        self._stopped = True
        ended = await cancel_call(call_sid=self.leg_sid)
        if not self.parallel_leg_id and self.parent_call_sid != self.leg_sid:
            # A single call: the rep was waiting alone in the room; release them to wrap-up.
            await cancel_call(call_sid=self.parent_call_sid)
        return ended


__all__ = ["TwilioHandlingLeg", "digits_body", "held_twiml", "join_url", "play_body"]
