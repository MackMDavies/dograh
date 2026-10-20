"""SignalWire side of the extra-answer chain (Dialer Phase 1b).

Same chain as Twilio (see ``extra_answer``), within what SignalWire's existing plumbing
allows:

* teammate — the auto-connect offer joins the person's private room
  (``sysevo-overflow-<uuid>`` for a parallel line; the existing ``sysevo-ringback-<uuid>``
  room after a missed ring-back). The person hears the original rep's hello clip first,
  never hold music. 8 s without a join moves the chain on (live SWML update).
* agent — the account's voice agent through the existing agent overflow SWML
  (``connect`` to the agent's number). SignalWire cannot hand that call back, so here
  the agent step is "agent talks"; when the person hangs up the lead becomes a due
  priority callback for the original rep (resolved as 'callback' when that step is on).
* apology — the rep's apology clip (else neutral speech), hang up, due callback.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger

from api.services.telephony.dialer import extra_answer
from api.services.telephony.dialer.inbound_call_log import notify_inbound_call
from api.services.telephony.dialer.swml import build_agent_overflow_swml, build_hangup_swml

_BACKGROUND: set[asyncio.Task] = set()
_MAX_FALLTHROUGH = 6


def _spawn(coro) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _play(url: str | None, fallback: str) -> dict:
    return {"play": {"url": url if url else f"say:{fallback}"}}


async def apology_swml(rep_user_id: str | None) -> dict:
    url = await extra_answer.clip_url(rep_user_id, "apology")
    return {"sections": {"main": [_play(url, extra_answer.NEUTRAL_APOLOGY), {"hangup": {}}]}}


async def teammate_swml(
    *, rep_user_id: str | None, conference_name: str, status_callback_url: str | None,
    disclose: bool,
) -> dict:
    """Hello clip, then the private room the teammate's softphone joins."""
    if not (conference_name.startswith("sysevo-overflow-") or conference_name.startswith("sysevo-ringback-")):
        raise ValueError("invalid SignalWire extra-answer room")
    steps: list[dict] = []
    if disclose:
        steps.append({"play": {"url": f"say:{extra_answer.RECORDING_DISCLOSURE}"}})
    steps.append(_play(await extra_answer.clip_url(rep_user_id, "hello"), extra_answer.NEUTRAL_HELLO))
    conference: dict[str, Any] = {"name": conference_name, "start_on_enter": False, "end_on_exit": True}
    if status_callback_url:
        if not status_callback_url.startswith("https://"):
            raise ValueError("SignalWire conference callbacks must use HTTPS")
        conference.update({
            "status_callback": status_callback_url,
            "status_callback_event": "join leave",
            "status_callback_event_type": "relay",
        })
    steps.append({"join_conference": conference})
    return {"sections": {"main": steps}}


async def _agent_number(rep_user_id: str | None) -> str:
    from api.services.telephony.dialer.inbound_agent import current_inbound_agent
    try:
        agent = await current_inbound_agent(user_id=rep_user_id)
    except Exception as exc:  # noqa: BLE001 - no agent: the chain moves on
        logger.info(f"SignalWire extra answer: agent lookup failed: {type(exc).__name__}")
        return ""
    return str(getattr(agent, "number", "") or "")


async def swml_for(
    state: dict[str, Any] | None, *, status_callback_url_for, disclose: bool, depth: int = 0,
) -> dict | None:
    """SWML for the step the chain just reserved. None: nothing to change."""
    if not state:
        return await apology_swml(None)
    action = state.get("action")
    rep = str(state.get("rep_user_id") or "") or None
    call_id = str(state.get("prospect_call_id") or "")
    if action == "teammate":
        room = str(state.get("conference_name") or "")
        if state.get("inbound_call_id"):
            notify_inbound_call(str(state["inbound_call_id"]))
        _spawn(expire_offer_later(call_id))
        callback = await status_callback_url_for(room) if room.startswith("sysevo-overflow-") else None
        return await teammate_swml(rep_user_id=rep, conference_name=room,
                                   status_callback_url=callback, disclose=disclose)
    if action == "agent":
        number = await _agent_number(rep)
        if number:
            await extra_answer.mark("signalwire", call_id, "agent_started")
            return build_agent_overflow_swml(
                agent_number=number, fallback_message=extra_answer.NEUTRAL_APOLOGY,
            )
        if depth >= _MAX_FALLTHROUGH:
            return await apology_swml(rep)
        following = await extra_answer.advance(provider="signalwire", prospect_call_id=call_id, after="agent")
        return await swml_for(following, status_callback_url_for=status_callback_url_for,
                              disclose=disclose, depth=depth + 1)
    if action == "apology":
        return await apology_swml(rep)
    if action == "hangup":
        return build_hangup_swml()
    return None


async def _overflow_callback_url(conference_name: str) -> str | None:
    from api.services.telephony.dialer import signalwire_routes
    try:
        return await signalwire_routes._signalwire_overflow_callback_url(conference_name)
    except Exception as exc:  # noqa: BLE001 - join timing is optional telemetry
        logger.info(f"SignalWire extra answer: no room callback: {type(exc).__name__}")
        return None


async def first_answer_swml(
    *, session_id: str, leg_id: str, provider_call_id: str, rep_call_id: str,
) -> dict:
    """A second human answered a SignalWire parallel batch whose rep is talking."""
    state = await extra_answer.begin(
        provider="signalwire", prospect_call_id=provider_call_id, source="parallel",
        parent_call_sid=rep_call_id or provider_call_id, leg_id=leg_id, session_id=session_id,
    )
    if not state:
        return await apology_swml(None)
    if state.get("status") != "active":
        return await apology_swml(state.get("rep_user_id")) if state.get("step") == "apology" else build_hangup_swml()
    if state.get("step") != "start":
        # A retried decision webhook: never reserve a second step.
        return await apology_swml(state.get("rep_user_id")) if state.get("step") == "apology" else build_hangup_swml()
    reserved = await extra_answer.advance(
        provider="signalwire", prospect_call_id=provider_call_id,
        conference_name=f"sysevo-overflow-{uuid.uuid4()}",
    )
    swml = await swml_for(reserved, status_callback_url_for=_overflow_callback_url, disclose=False)
    return swml or build_hangup_swml()


async def continue_after(provider_call_id: str, *, after: str) -> dict[str, Any] | None:
    """Move a LIVE SignalWire call on from ``after`` and hand it the next step's SWML."""
    state = await extra_answer.advance(provider="signalwire", prospect_call_id=provider_call_id, after=after)
    if not state or state.get("action") == "none":
        return state
    if state.get("action") == "teammate":
        # Still in the same room (the ring-back's): only the offer moved.
        if state.get("inbound_call_id"):
            notify_inbound_call(str(state["inbound_call_id"]))
        _spawn(expire_offer_later(provider_call_id))
        return state
    swml = await swml_for(state, status_callback_url_for=_overflow_callback_url, disclose=False)
    if swml:
        from api.services.telephony.dialer.signalwire_calling import update_call_swml
        try:
            await update_call_swml(call_id=provider_call_id, swml=swml)
        except Exception as exc:  # noqa: BLE001 - the call may have ended
            logger.warning(f"SignalWire extra answer: live update failed: {type(exc).__name__}")
    return state


async def expire_offer_later(provider_call_id: str, *, delay: float = extra_answer.OFFER_SECONDS + 0.5) -> None:
    await asyncio.sleep(delay)
    try:
        await continue_after(provider_call_id, after="teammate")
    except Exception as exc:  # noqa: BLE001 - the reconciliation worker retries
        logger.warning(f"SignalWire extra-answer expiry deferred: {type(exc).__name__}")


async def continue_after_ringback(*, rep_call_id: str, provider_call_id: str) -> dict[str, Any] | None:
    """The original rep did not take their ring-back: carry on from the teammate step."""
    state = await extra_answer.begin(
        provider="signalwire", prospect_call_id=provider_call_id, source="ringback",
        parent_call_sid=rep_call_id,
    )
    if not state:
        return None
    return await continue_after(provider_call_id, after="ringback")
