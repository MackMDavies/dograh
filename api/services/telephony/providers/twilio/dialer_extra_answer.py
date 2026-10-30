"""Twilio side of the extra-answer chain (Dialer Phase 1b).

A person answered and their rep is not free: a second human on a parallel batch, or a
person who picked up after the rep pressed "Send voicemail" and the ring-back to that rep
was missed. Hold is gone; the person never hears music, silence or "please hold". The
chain (see ``api.services.telephony.dialer.extra_answer``) decides the step; this module
turns each step into TwiML or a live-call update:

* teammate — the person waits in ``call-<their SID>``; while the teammate's softphone
  auto-answers they hear the ORIGINAL rep's hello clip (``waitUrl``), else a short
  neutral "Hi, one moment.". 8 s without a join moves the chain on.
* agent — the existing outbound-agent stream (dialer_agent_handoff). For 90 s the
  reconciliation worker offers the call back (auto-connect) to whoever frees up first,
  the original rep before teammates; once they are in ``call-<SID>`` the agent says so
  and the pipeline's own transfer strategy moves the person in (live_control
  ``dialer_transfer``). If nobody frees up, the agent is asked to agree a callback time.
* apology — the rep's apology clip (else neutral speech), then hang up; the lead is a
  due priority callback for the original rep.

dialer_hold is never used here; it stays only for calls held before this release.
"""

from __future__ import annotations

import asyncio
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from api.services.telephony.dialer import extra_answer
from api.services.telephony.dialer.inbound_call_log import notify_inbound_call
from api.services.telephony.providers.twilio import dialer_agent_handoff
from api.services.telephony.providers.twilio.dialer_conference import _twilio_client
from api.services.telephony.providers.twilio.dialer_twiml import conference_telemetry_attributes
from api.utils.common import get_backend_endpoints

HANGUP = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
_XML = '<?xml version="1.0" encoding="UTF-8"?>'
_MAX_FALLTHROUGH = 6
# The longest quiet the person ever hears while a softphone joins.
WAIT_PAUSE_SECONDS = 2
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _say(text: str) -> str:
    return f"<Say>{escape(text)}</Say>"


async def endpoint_root() -> str:
    backend, _ = await get_backend_endpoints()
    return str(backend or "").rstrip("/") + "/api/v1/telephony"


def goodbye_twiml() -> str:
    """The polite end when the apology step is off: spoken goodbye, then hang up."""
    return f"{_XML}<Response>{_say(extra_answer.NEUTRAL_GOODBYE)}<Hangup/></Response>"


async def apology_twiml(rep_user_id: str | None) -> str:
    url = await extra_answer.clip_url(rep_user_id, "apology")
    body = f"<Play>{escape(url)}</Play>" if url else _say(extra_answer.NEUTRAL_APOLOGY)
    return f"{_XML}<Response>{body}<Hangup/></Response>"


def conference_twiml(*, prospect_call_sid: str, root: str, disclose: bool) -> str:
    """Park the person in their own room while a teammate's softphone joins it."""
    attrs = conference_telemetry_attributes(
        events_url=f"{root}/dialer-conference-events",
        recording_url=f"{root}/dialer-recording-callback",
    )
    notice = _say(extra_answer.RECORDING_DISCLOSURE) if disclose and extra_answer.AUTO_RECORDING_NOTICE else ""
    return (
        f"{_XML}<Response>{notice}<Dial><Conference {attrs}"
        'startConferenceOnEnter="false" endConferenceOnExit="true" beep="false" '
        f'waitUrl={quoteattr(root + "/dialer-extra-answer-wait?call=" + prospect_call_sid)} waitMethod="POST">'
        f"{escape('call-' + prospect_call_sid)}</Conference></Dial></Response>"
    )


async def hello_wait_twiml(prospect_call_sid: str) -> str:
    """What the person hears for the second or two a softphone takes to join.

    The original rep's hello clip ("Hi, it's Max, one second"), else a neutral line, then a
    short spoken line repeated with pauses of at most 2 s. Never music and never a long
    silence. Twilio replays a waitUrl document when it ends; the replay is the same
    reassurance, so a longer wait still never goes quiet.
    """
    owner = await extra_answer.offer_owner(provider="twilio", prospect_call_id=prospect_call_sid)
    url = await extra_answer.clip_url(owner, "hello") if owner else None
    hello = f"<Play>{escape(url)}</Play>" if url else _say(extra_answer.NEUTRAL_HELLO)
    connecting = (f'<Pause length="{WAIT_PAUSE_SECONDS}"/>' + _say(extra_answer.CONNECTING_LINE)) * 6
    return f"{_XML}<Response>{hello}{connecting}</Response>"


async def _update_live_call(call_sid: str, twiml: str, account_sid: str | None) -> bool:
    try:
        client = await _twilio_client()
        if not client or (account_sid and client.account_sid != account_sid):
            logger.warning("Extra answer: the original Twilio account is unavailable")
            return False
        await asyncio.to_thread(client.calls(call_sid).update, twiml=twiml)
        return True
    except Exception as exc:  # noqa: BLE001 - the call may have ended meanwhile
        logger.warning(f"Extra answer: could not update the live call: {type(exc).__name__}")
        return False


async def twiml_for(
    state: dict[str, Any] | None, *, root: str, account_sid: str, disclose: bool,
    rep_user_id: str | None = None, depth: int = 0,
) -> str | None:
    """TwiML for the step the chain just reserved. None: nothing to change."""
    if not state:
        return await apology_twiml(rep_user_id)
    action = state.get("action")
    sid = str(state.get("prospect_call_id") or "")
    rep_user_id = str(state.get("rep_user_id") or rep_user_id or "") or None
    if action == "teammate":
        if state.get("inbound_call_id"):
            notify_inbound_call(str(state["inbound_call_id"]))
        _spawn(expire_offer_later(
            sid, account_sid=account_sid,
            delay=extra_answer.offer_seconds(disclose=disclose) + 0.5,
        ))
        return conference_twiml(prospect_call_sid=sid, root=root, disclose=disclose)
    if action == "agent":
        prepared = None
        try:
            prepared = await dialer_agent_handoff.prepare_extra_answer_agent(
                prospect_call_sid=sid, account_sid=account_sid, disclose=disclose,
            )
        except dialer_agent_handoff.AgentHandoffUnavailable as exc:
            logger.warning(f"Extra answer: the voice agent is unavailable ({exc})")
        if prepared:
            return None if prepared.already_streaming else prepared.twiml
        if depth >= _MAX_FALLTHROUGH:
            return await apology_twiml(rep_user_id)
        following = await extra_answer.advance(provider="twilio", prospect_call_id=sid, after="agent")
        return await twiml_for(
            following, root=root, account_sid=account_sid, disclose=disclose,
            rep_user_id=rep_user_id, depth=depth + 1,
        )
    if action == "apology":
        return await apology_twiml(rep_user_id)
    if action in {"goodbye", "hangup"}:
        # Never a bare hang-up: the person always hears why the call ends.
        return goodbye_twiml()
    return None


async def first_answer_twiml(
    *, parent_call_sid: str, child_call_sid: str, leg_id: str, account_sid: str, root: str,
) -> str:
    """A second human answered a parallel batch whose rep is already talking."""
    state = await extra_answer.begin(
        provider="twilio", prospect_call_id=child_call_sid, source="parallel",
        parent_call_sid=parent_call_sid, leg_id=leg_id, account_sid=account_sid,
    )
    if not state:
        # Not a line the chain can own (or storage is down): apologise and end, never park.
        return await apology_twiml(None)
    step = state.get("step")
    if state.get("status") != "active":
        return await apology_twiml(state.get("rep_user_id")) if step == "apology" else goodbye_twiml()
    if step == "start":
        # The person hears the recording notice first: the offer runs ~3 s after it.
        reserved = await extra_answer.advance(
            provider="twilio", prospect_call_id=child_call_sid,
            offer_seconds=extra_answer.offer_seconds(disclose=True),
        )
        twiml = await twiml_for(reserved, root=root, account_sid=account_sid, disclose=True,
                                rep_user_id=state.get("rep_user_id"))
        return twiml or HANGUP
    # A retried answer webhook: repeat the step already reserved, never take another.
    if step == "teammate":
        return conference_twiml(prospect_call_sid=child_call_sid, root=root, disclose=True)
    twiml = await twiml_for({**state, "action": "agent" if step == "agent" else "apology"},
                            root=root, account_sid=account_sid, disclose=True)
    return twiml or goodbye_twiml()


async def ringback_unavailable_twiml(
    *, parent_call_sid: str, prospect_call_sid: str, account_sid: str, root: str,
) -> str | None:
    """A person answered after the rep handed the line over, and the rep is not free.

    The extra-answer chain starts straight away (teammate → agent → apology / goodbye)
    instead of hanging up. None: the chain cannot own this call (the caller then keeps
    today's behaviour).
    """
    state = await extra_answer.begin(
        provider="twilio", prospect_call_id=prospect_call_sid, source="ringback",
        parent_call_sid=parent_call_sid, account_sid=account_sid,
    )
    if not state:
        return None
    if state.get("status") != "active" or state.get("step") != "start":
        # A retried answer webhook: repeat what was reserved, never reserve another step.
        if state.get("step") == "teammate":
            return conference_twiml(prospect_call_sid=prospect_call_sid, root=root, disclose=True)
        if state.get("status") != "active":
            return await apology_twiml(state.get("rep_user_id")) if state.get("step") == "apology" else goodbye_twiml()
    reserved = await extra_answer.advance(
        provider="twilio", prospect_call_id=prospect_call_sid, after="ringback",
        offer_seconds=extra_answer.offer_seconds(disclose=True),
    )
    return await twiml_for(
        reserved, root=root, account_sid=account_sid, disclose=True,
        rep_user_id=str(state.get("rep_user_id") or "") or None,
    )


async def continue_after(
    prospect_call_sid: str, *, after: str, account_sid: str | None = None,
) -> dict[str, Any] | None:
    """Move a LIVE call on from ``after`` (an expired offer) and apply the next step."""
    state = await extra_answer.advance(
        provider="twilio", prospect_call_id=prospect_call_sid, after=after,
        offer_seconds=extra_answer.OFFER_SECONDS,
    )
    if not state or state.get("action") == "none":
        return state
    account = account_sid or str(state.get("account_sid") or "")
    if state.get("action") == "teammate":
        # The person is already waiting in call-<SID>: only the offer moved.
        if state.get("inbound_call_id"):
            notify_inbound_call(str(state["inbound_call_id"]))
        _spawn(expire_offer_later(
            prospect_call_sid, account_sid=account, delay=extra_answer.OFFER_SECONDS + 0.5,
        ))
        return state
    twiml = await twiml_for(state, root=await endpoint_root(), account_sid=account, disclose=False)
    if twiml:
        await _update_live_call(prospect_call_sid, twiml, account or None)
    return state


async def expire_offer_later(
    prospect_call_sid: str, *, account_sid: str | None,
    delay: float = extra_answer.OFFER_SECONDS + 0.5,
) -> None:
    """Nobody joined in time: move on. Atomic with a late join (SQL row lock)."""
    await asyncio.sleep(delay)
    try:
        await continue_after(prospect_call_sid, after="teammate", account_sid=account_sid)
    except Exception as exc:  # noqa: BLE001 - the reconciliation worker retries
        logger.warning(f"Extra-answer offer expiry deferred: {type(exc).__name__}")


async def continue_after_ringback(*, parent_call_sid: str, prospect_call_sid: str) -> dict[str, Any] | None:
    """The original rep did not take their ring-back: carry on from the teammate step."""
    state = await extra_answer.begin(
        provider="twilio", prospect_call_id=prospect_call_sid, source="ringback",
        parent_call_sid=parent_call_sid,
    )
    if not state:
        return None
    return await continue_after(prospect_call_sid, after="ringback")


async def hand_over(prospect_call_sid: str) -> bool:
    """The transfer-back target is in the room: ask the agent to hand over (once)."""
    from api.services.supervisor import live_control

    claim = await extra_answer.claim_transfer(prospect_call_sid)
    if not claim or not str(claim.get("workflow_run_id") or "").isdigit():
        return False
    receivers = 0
    try:
        receivers = await live_control.publish_command(
            str(claim["workflow_run_id"]),
            live_control.dialer_transfer_command(
                conference_name=str(claim.get("conference_name") or ""),
                to_name=str(claim.get("user_name") or ""),
                to_user_id=str(claim.get("user_id") or ""),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - retried by the worker
        logger.warning(f"Extra answer: could not reach the agent run: {type(exc).__name__}")
    if not receivers:
        await extra_answer.release_transfer(prospect_call_sid)
        return False
    return True


async def request_summary(state: dict[str, Any]) -> int:
    """Ask the agent's pipeline to store its summary (it holds the conversation)."""
    run_id = str(state.get("workflow_run_id") or "")
    if not run_id.isdigit():
        return 0
    from api.services.supervisor import live_control
    try:
        return await live_control.publish_command(run_id, live_control.dialer_summary_command())
    except Exception as exc:  # noqa: BLE001 - the offer goes out without it
        logger.info(f"Extra answer: summary request not delivered: {type(exc).__name__}")
        return 0


def _seconds_since(value: Any) -> float | None:
    from datetime import UTC, datetime
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - at).total_seconds()


async def _rep_leg_for_offer(state: dict[str, Any]) -> str | None:
    """The joined rep's own leg for this call's offer (to release them on a failed handover)."""
    import httpx
    from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
    offer = str(state.get("offer_inbound_call_id") or "")
    if not offer or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/inbound_calls",
                params={"select": "rep_provider_call_id", "id": f"eq.{offer}", "limit": "1"},
                headers={"apikey": SUPABASE_SERVICE_ROLE_KEY,
                         "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info(f"Extra answer: rep leg lookup failed: {type(exc).__name__}")
        return None
    value = rows[0].get("rep_provider_call_id") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    return str(value) if isinstance(value, str) and value.startswith("CA") else None


async def recover_transfer(state: dict[str, Any]) -> str:
    """The agent was asked to hand over but the person never reached the rep's room.

    First attempt: move the person into call-<SID> ourselves (the rep is waiting there).
    If that is impossible (their call is gone) or it also does not land, release the rep's
    leg so nobody is left alone, and settle the call as a callback for the original rep.
    """
    sid = str(state.get("prospect_call_id") or "")
    account = str(state.get("account_sid") or "") or None
    if not state.get("transfer_recovery_at"):
        await extra_answer.mark("twilio", sid, "transfer_recovery")
        root = await endpoint_root()
        if await _update_live_call(sid, conference_twiml(prospect_call_sid=sid, root=root, disclose=False), account):
            return "redirected"
    rep_leg = await _rep_leg_for_offer(state)
    if rep_leg:
        from api.services.telephony.providers.twilio.dialer_conference import cancel_call
        await cancel_call(call_sid=rep_leg)
    await extra_answer.mark("twilio", sid, "ended")
    return "released"


async def drive_agent(state: dict[str, Any]) -> str | None:
    """One tick of the agent's transfer-back window (reconciliation worker)."""
    sid = str(state.get("prospect_call_id") or "")
    requested_ago = _seconds_since(state.get("transfer_requested_at"))
    if requested_ago is not None:
        # The handover was asked for: it lands when the person joins the rep's room.
        recovery_ago = _seconds_since(state.get("transfer_recovery_at"))
        if requested_ago >= extra_answer.TRANSFER_LAND_SECONDS and (
            recovery_ago is None or recovery_ago >= extra_answer.TRANSFER_LAND_SECONDS
        ):
            return await recover_transfer(state)
        return "handing_over"
    # The pipeline refreshes the stored summary every tick; the offer carries the stored
    # one. Before the first summary exists, write it first and offer on the next tick.
    delivered = await request_summary(state)
    if delivered and not state.get("agent_summary") and (
        _seconds_since(state.get("agent_started_at")) or 0.0
    ) < extra_answer.TRANSFER_LAND_SECONDS:
        return "summarizing"
    reserved = await extra_answer.reserve_transfer(sid)
    status = (reserved or {}).get("status")
    if status == "offered" and reserved.get("inbound_call_id"):
        notify_inbound_call(str(reserved["inbound_call_id"]))
    elif status == "joined":
        await hand_over(sid)
    elif status == "deadline" and not state.get("callback_requested_at"):
        run_id = str(state.get("workflow_run_id") or "")
        if state.get("callback_in_chain") and run_id.isdigit():
            from api.services.supervisor import live_control
            try:
                await live_control.publish_command(run_id, {
                    "type": "whisper", "text": extra_answer.callback_whisper(state.get("rep_name")),
                })
            except Exception as exc:  # noqa: BLE001 - the agent carries on regardless
                logger.info(f"Extra answer: callback instruction not delivered: {type(exc).__name__}")
        await extra_answer.mark("twilio", sid, "callback_requested")
    return status


async def on_prospect_joined(*, conference_name: str, call_sid: str) -> None:
    """The person's own leg joined call-<their SID>: an agent handover has landed."""
    if not call_sid or conference_name != f"call-{call_sid}":
        return
    try:
        await extra_answer.prospect_joined(provider="twilio", prospect_call_id=call_sid)
    except Exception as exc:  # noqa: BLE001 - evidence; the worker recovers a stuck handover
        logger.info(f"Extra answer: prospect join not recorded: {type(exc).__name__}")


async def on_rep_joined(*, conference_name: str, rep_user_id: str) -> None:
    """A softphone joined call-<SID>: a teammate took the call, or the agent's
    transfer-back target arrived and the handover can start now."""
    try:
        state = await extra_answer.record_rep_joined(
            provider="twilio", conference_name=conference_name, rep_user_id=rep_user_id,
        )
        if state and state.get("transfer") is True:
            await hand_over(str(state.get("prospect_call_id") or ""))
    except Exception as exc:  # noqa: BLE001 - evidence; the worker retries the handover
        logger.info(f"Extra answer: join not recorded: {type(exc).__name__}")
