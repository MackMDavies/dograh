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


def goodbye_swml() -> dict:
    """The polite end when the apology step is off: spoken goodbye, then hang up."""
    return {"sections": {"main": [_play(None, extra_answer.NEUTRAL_GOODBYE), {"hangup": {}}]}}


# The longest quiet the person hears while a softphone joins the room.
WAIT_PAUSE_SECONDS = 2
WAIT_PATH = "sw-extra-answer-wait"


def wait_swml() -> dict:
    """What the room plays until the softphone joins: a short spoken line with pauses of
    at most 2 s. Replaces SignalWire's default hold music (join_conference wait_url)."""
    steps: list[dict] = []
    for _ in range(8):
        steps.append({"play": {"url": f"silence:{WAIT_PAUSE_SECONDS}"}})
        steps.append({"play": {"url": f"say:{extra_answer.CONNECTING_LINE}"}})
    return {"sections": {"main": steps}}


async def _wait_url(conference_name: str) -> str | None:
    """Our signed wait_url for this room (never default hold music)."""
    from api.services.telephony.dialer import signalwire_routes
    from api.utils.common import get_backend_endpoints
    try:
        backend_endpoint, _ = await get_backend_endpoints()
        url = signalwire_routes._webhook_url(str(backend_endpoint or "").rstrip("/"), WAIT_PATH, conference_name)
    except Exception as exc:  # noqa: BLE001 - the room still joins; logged
        logger.warning(f"SignalWire extra answer: no wait URL: {type(exc).__name__}")
        return None
    return url if url.startswith("https://") else None


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
    wait_url = await _wait_url(conference_name)
    if wait_url:
        conference["wait_url"] = wait_url
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
        _spawn(expire_offer_later(call_id, delay=extra_answer.offer_seconds(disclose=disclose) + 0.5))
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
    if action in {"goodbye", "hangup"}:
        # Never a bare hang-up: the person always hears why the call ends.
        return goodbye_swml()
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
        return await apology_swml(state.get("rep_user_id")) if state.get("step") == "apology" else goodbye_swml()
    if state.get("step") != "start":
        # A retried decision webhook: never reserve a second step.
        return await apology_swml(state.get("rep_user_id")) if state.get("step") == "apology" else goodbye_swml()
    reserved = await extra_answer.advance(
        provider="signalwire", prospect_call_id=provider_call_id,
        conference_name=f"sysevo-overflow-{uuid.uuid4()}",
    )
    swml = await swml_for(reserved, status_callback_url_for=_overflow_callback_url, disclose=False)
    return swml or build_hangup_swml()


async def continue_after(provider_call_id: str, *, after: str) -> dict[str, Any] | None:
    """Move a LIVE SignalWire call on from ``after`` and hand it the next step's SWML."""
    state = await extra_answer.advance(
        provider="signalwire", prospect_call_id=provider_call_id, after=after,
        offer_seconds=extra_answer.OFFER_SECONDS,
    )
    if not state or state.get("action") == "none":
        return state
    if state.get("action") == "teammate":
        # Still in the same room (the ring-back's): only the offer moved.
        if state.get("inbound_call_id"):
            notify_inbound_call(str(state["inbound_call_id"]))
        _spawn(expire_offer_later(provider_call_id, delay=extra_answer.OFFER_SECONDS + 0.5))
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


async def ringback_unavailable_swml(*, rep_call_id: str, provider_call_id: str) -> dict | None:
    """A person answered after the rep handed the line over, and the rep is not free.

    The extra-answer chain starts straight away (teammate in a fresh room → agent →
    apology / goodbye) instead of hanging up. None: the chain cannot own this call.
    """
    state = await extra_answer.begin(
        provider="signalwire", prospect_call_id=provider_call_id, source="ringback",
        parent_call_sid=rep_call_id,
    )
    if not state:
        return None
    if state.get("status") != "active" or state.get("step") != "start":
        return await apology_swml(state.get("rep_user_id")) if state.get("step") == "apology" else goodbye_swml()
    reserved = await extra_answer.advance(
        provider="signalwire", prospect_call_id=provider_call_id, after="ringback",
        conference_name=f"sysevo-overflow-{uuid.uuid4()}",
        offer_seconds=extra_answer.offer_seconds(disclose=True),
    )
    return await swml_for(reserved, status_callback_url_for=_overflow_callback_url, disclose=True)
