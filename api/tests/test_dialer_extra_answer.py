"""Dialer Phase 1b: the extra-answer chain ("an extra person is on the line").

Hold is gone. A person who answers while their rep is not free is walked through the
account's chain (teammate auto-connect → voice agent with transfer-back → agent-booked
callback → apology) and never hears music, silence or "please hold". Postgres owns the
decisions (dialer_extra_answer_*); these tests cover the provider side: what each step
becomes on Twilio and SignalWire, ordering and skipped steps, the ring-back continuation,
the agent's transfer-back window, the worker, and the adaptive line cap.

No real calls, no provider or Supabase requests: every seam is patched.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.supervisor import live_control
from api.services.telephony.dialer import call_stages, extra_answer, signalwire_extra_answer
from api.services.telephony.providers.twilio import dialer_extra_answer, dialer_voicemail_ringback

_XA = "api.services.telephony.dialer.extra_answer"
_TXA = "api.services.telephony.providers.twilio.dialer_extra_answer"
_SXA = "api.services.telephony.dialer.signalwire_extra_answer"
_RINGBACK = "api.services.telephony.providers.twilio.dialer_voicemail_ringback"
_SID = "CA" + "1" * 32
_PARENT = "CA" + "9" * 32
_REP = "11111111-1111-4111-8111-111111111111"
_MATE = "22222222-2222-4222-8222-222222222222"
_ACCOUNT = "AC" + "3" * 32


def _closing_spawn():
    spawned = []

    def spawn(coro):
        spawned.append(getattr(coro, "__name__", ""))
        coro.close()

    return spawned, spawn


# ── extra_answer (provider-neutral) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_advance_and_begin_refuse_unknown_steps_and_sources():
    with patch(f"{_XA}._rpc_dict", new=AsyncMock()) as rpc:
        assert await extra_answer.advance(provider="twilio", prospect_call_id=_SID, after="hold") is None
        assert await extra_answer.begin(provider="twilio", prospect_call_id=_SID, source="hold",
                                        parent_call_sid=_PARENT) is None
        assert await extra_answer.mark("twilio", _SID, "held") is None
    rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_advance_passes_the_failed_step_and_rejects_unknown_actions():
    with patch(f"{_XA}._rpc_dict", new=AsyncMock(return_value={"action": "teammate"})) as rpc:
        assert (await extra_answer.advance(provider="twilio", prospect_call_id=_SID, after="ringback"))["action"] == "teammate"
    assert rpc.await_args.args == ("dialer_extra_answer_advance", {
        "p_provider": "twilio", "p_prospect_call_id": _SID, "p_after": "ringback", "p_conference_name": None,
    })
    with patch(f"{_XA}._rpc_dict", new=AsyncMock(return_value={"action": "hold"})):
        assert await extra_answer.advance(provider="twilio", prospect_call_id=_SID) is None


@pytest.mark.asyncio
async def test_clip_url_only_signs_the_reps_own_clip():
    sign = AsyncMock(return_value="https://storage.example.com/signed")
    with patch(f"{_XA}._rpc_dict", new=AsyncMock(return_value={
        "storage_path": f"{_REP}/hello-1.wav", "owner_user_id": _REP})), \
         patch("api.services.telephony.providers.twilio.dialer_call_log.sign_voicemail_storage_path", new=sign):
        assert await extra_answer.clip_url(_REP, "hello") == "https://storage.example.com/signed"
    with patch(f"{_XA}._rpc_dict", new=AsyncMock(return_value={
        "storage_path": f"{_MATE}/hello-1.wav", "owner_user_id": _MATE})), \
         patch("api.services.telephony.providers.twilio.dialer_call_log.sign_voicemail_storage_path", new=sign):
        assert await extra_answer.clip_url(_REP, "hello") is None
    assert await extra_answer.clip_url(_REP, "voicemail") is None


@pytest.mark.asyncio
async def test_rep_joined_resolves_the_twilio_room_to_the_persons_call():
    with patch(f"{_XA}.mark", new=AsyncMock(return_value={"transfer": True})) as mark:
        state = await extra_answer.record_rep_joined(provider="twilio", conference_name=f"call-{_SID}", rep_user_id=_MATE)
    assert state == {"transfer": True}
    mark.assert_awaited_once_with("twilio", _SID, "rep_joined", {"user_id": _MATE})
    with patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        assert await extra_answer.record_rep_joined(provider="twilio", conference_name="sysevo-par-x", rep_user_id=_MATE) is None
    mark.assert_not_awaited()


@pytest.mark.parametrize("cap,expected", [(None, 5), (10, 5), (5, 5), (3, 3), (2, 2), (4, 3)])
def test_line_cap_trims_to_a_supported_line_count_in_queue_order(cap, expected):
    entries = [f"e{i}" for i in range(5)]
    trimmed = extra_answer.trim_to_line_cap(entries, cap)
    assert trimmed == entries[:expected]


@pytest.mark.asyncio
async def test_line_cap_is_the_strictest_list_and_fails_open():
    class Response:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=Response([{"list_id": "l1"}, {"list_id": "l2"}, {"list_id": "l1"}]))
    client.post = AsyncMock(side_effect=[Response(5), Response(2)])
    with patch(f"{_XA}._ready", return_value=True), patch(f"{_XA}.httpx.AsyncClient", return_value=client):
        assert await extra_answer.parallel_line_cap(["a", "b", "c"]) == 2
    assert [c.kwargs["json"] for c in client.post.await_args_list] == [{"p_list_id": "l1"}, {"p_list_id": "l2"}]
    client.get = AsyncMock(side_effect=RuntimeError("down"))
    with patch(f"{_XA}._ready", return_value=True), patch(f"{_XA}.httpx.AsyncClient", return_value=client):
        assert await extra_answer.parallel_line_cap(["a"]) is None


def test_chain_stages_keep_the_reps_status_from_saying_call_them_back():
    for stage in ("extra_answer", "teammate_connected", "agent_bridging", "agent_transferred"):
        payload = call_stages.with_stage({"state": "rejected", "failure": "person_answered"},
                                         {"stage": stage, "stage_at": "t"})
        assert payload["state"] == "ringback" and "failure" not in payload
    for stage in ("callback_booked", "apology_played"):
        payload = call_stages.with_stage({"state": "rejected", "failure": "person_answered"},
                                         {"stage": stage, "stage_at": "t"})
        assert payload["state"] == "rejected"
    assert {"extra_answer", "teammate_connected", "agent_bridging", "agent_transferred",
            "callback_booked", "apology_played"} <= call_stages.STAGES


# ── Twilio: TwiML per step ──────────────────────────────────────────────────

def test_teammate_room_never_plays_hold_music():
    twiml = dialer_extra_answer.conference_twiml(prospect_call_sid=_SID, root="https://api.example.com/t", disclose=False)
    assert f'waitUrl="https://api.example.com/t/dialer-extra-answer-wait?call={_SID}"' in twiml
    assert 'startConferenceOnEnter="false"' in twiml and 'endConferenceOnExit="true"' in twiml
    assert f">call-{_SID}</Conference>" in twiml
    assert "<Say>" not in twiml


@pytest.mark.asyncio
async def test_wait_plays_the_original_reps_hello_clip():
    with patch(f"{_XA}.offer_owner", new=AsyncMock(return_value=_REP)) as owner, \
         patch(f"{_XA}.clip_url", new=AsyncMock(return_value="https://storage.example.com/hi.wav")) as clip:
        twiml = await dialer_extra_answer.hello_wait_twiml(_SID)
    owner.assert_awaited_once_with(provider="twilio", prospect_call_id=_SID)
    clip.assert_awaited_once_with(_REP, "hello")
    assert twiml.index("<Play>https://storage.example.com/hi.wav</Play>") < twiml.index("<Pause")
    assert "hold" not in twiml.lower()


@pytest.mark.asyncio
async def test_wait_without_a_clip_says_a_short_neutral_hello():
    with patch(f"{_XA}.offer_owner", new=AsyncMock(return_value=None)), \
         patch(f"{_XA}.clip_url", new=AsyncMock()) as clip:
        twiml = await dialer_extra_answer.hello_wait_twiml(_SID)
    clip.assert_not_awaited()
    assert "<Say>Hi, one moment.</Say>" in twiml and "<Play>" not in twiml


@pytest.mark.asyncio
async def test_live_call_moves_from_an_expired_teammate_offer_to_the_agent():
    prepared = SimpleNamespace(twiml="<Response><Connect/></Response>", already_streaming=False)
    with patch(f"{_XA}.advance", new=AsyncMock(return_value={
             "action": "agent", "prospect_call_id": _SID, "rep_user_id": _REP, "account_sid": _ACCOUNT})) as adv, \
         patch(f"{_TXA}.endpoint_root", new=AsyncMock(return_value="https://api.example.com/t")), \
         patch(f"{_TXA}.dialer_agent_handoff.prepare_extra_answer_agent", new=AsyncMock(return_value=prepared)) as adopt, \
         patch(f"{_TXA}._update_live_call", new=AsyncMock(return_value=True)) as update:
        await dialer_extra_answer.continue_after(_SID, after="teammate")
    adv.assert_awaited_once_with(provider="twilio", prospect_call_id=_SID, after="teammate")
    # The person already heard the recording notice when they answered.
    adopt.assert_awaited_once_with(prospect_call_sid=_SID, account_sid=_ACCOUNT, disclose=False)
    update.assert_awaited_once_with(_SID, prepared.twiml, _ACCOUNT)


@pytest.mark.asyncio
async def test_live_call_gets_the_apology_when_nothing_else_is_possible():
    with patch(f"{_XA}.advance", new=AsyncMock(return_value={
             "action": "apology", "prospect_call_id": _SID, "rep_user_id": _REP})), \
         patch(f"{_TXA}.endpoint_root", new=AsyncMock(return_value="https://x")), \
         patch(f"{_XA}.clip_url", new=AsyncMock(return_value=None)), \
         patch(f"{_TXA}._update_live_call", new=AsyncMock(return_value=True)) as update:
        await dialer_extra_answer.continue_after(_SID, after="teammate", account_sid=_ACCOUNT)
    twiml = update.await_args.args[1]
    assert "Sorry, I've just been pulled onto another call." in twiml and twiml.endswith("<Hangup/></Response>")


@pytest.mark.asyncio
async def test_a_new_teammate_offer_leaves_the_waiting_person_where_they_are():
    spawned, spawn = _closing_spawn()
    with patch(f"{_XA}.advance", new=AsyncMock(return_value={
             "action": "teammate", "prospect_call_id": _SID, "inbound_call_id": "ic-2"})), \
         patch(f"{_TXA}.notify_inbound_call") as notify, \
         patch(f"{_TXA}._spawn", side_effect=spawn), \
         patch(f"{_TXA}._update_live_call", new=AsyncMock()) as update:
        await dialer_extra_answer.continue_after(_SID, after="ringback")
    notify.assert_called_once_with("ic-2")
    assert spawned == ["expire_offer_later"]
    update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, {"action": "none", "prospect_call_id": _SID}])
async def test_a_step_someone_else_already_moved_on_changes_nothing(state):
    with patch(f"{_XA}.advance", new=AsyncMock(return_value=state)), \
         patch(f"{_TXA}._update_live_call", new=AsyncMock()) as update, \
         patch(f"{_TXA}.notify_inbound_call") as notify:
        await dialer_extra_answer.continue_after(_SID, after="teammate")
    update.assert_not_awaited()
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_offer_timer_moves_on_from_the_teammate_step():
    with patch(f"{_TXA}.continue_after", new=AsyncMock()) as cont:
        await dialer_extra_answer.expire_offer_later(_SID, account_sid=_ACCOUNT, delay=0)
    cont.assert_awaited_once_with(_SID, after="teammate", account_sid=_ACCOUNT)


@pytest.mark.asyncio
async def test_agent_failure_falls_through_in_chain_order():
    advance = AsyncMock(side_effect=[{"action": "hangup", "prospect_call_id": _SID}])
    with patch(f"{_TXA}.dialer_agent_handoff.prepare_extra_answer_agent", new=AsyncMock(return_value=None)), \
         patch(f"{_XA}.advance", new=advance):
        twiml = await dialer_extra_answer.twiml_for(
            {"action": "agent", "prospect_call_id": _SID, "rep_user_id": _REP},
            root="https://x", account_sid=_ACCOUNT, disclose=True,
        )
    advance.assert_awaited_once_with(provider="twilio", prospect_call_id=_SID, after="agent")
    assert twiml == dialer_extra_answer.HANGUP


@pytest.mark.asyncio
async def test_agent_already_streaming_needs_no_update():
    prepared = SimpleNamespace(twiml="", already_streaming=True)
    with patch(f"{_TXA}.dialer_agent_handoff.prepare_extra_answer_agent", new=AsyncMock(return_value=prepared)):
        assert await dialer_extra_answer.twiml_for(
            {"action": "agent", "prospect_call_id": _SID}, root="https://x", account_sid=_ACCOUNT, disclose=False,
        ) is None


def test_hold_is_unreachable_from_the_extra_answer_path():
    import inspect

    source = inspect.getsource(dialer_extra_answer)
    assert "dialer_hold." not in source and "hold_twiml" not in source
    from api.services.telephony.providers.twilio import routes
    route_source = inspect.getsource(routes.handle_dialer_conference_join)
    assert "hold_twiml" not in route_source and "route_parallel_to_teammate" not in route_source


# ── Twilio: ring-back continues the chain ───────────────────────────────────

@pytest.mark.asyncio
async def test_missed_ringback_continues_with_a_teammate_and_keeps_the_person():
    with patch(f"{_RINGBACK}.expire_unanswered_inbound_call", new=AsyncMock(return_value=True)), \
         patch(f"{_RINGBACK}.dialer_extra_answer.continue_after_ringback", new=AsyncMock(
             return_value={"action": "teammate"})) as cont, \
         patch(f"{_RINGBACK}.cancel_call", new=AsyncMock()) as cancel, \
         patch(f"{_RINGBACK}.record_voicemail_playback_failure", new=AsyncMock()) as failed:
        await dialer_voicemail_ringback.expire_ringback(parent_call_sid=_PARENT, prospect_call_sid=_SID, delay_seconds=0)
    cont.assert_awaited_once_with(parent_call_sid=_PARENT, prospect_call_sid=_SID)
    cancel.assert_not_awaited()
    failed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["apology", "hangup"])
async def test_missed_ringback_that_ends_in_the_apology_tells_the_rep_to_call_back(action):
    with patch(f"{_RINGBACK}.expire_unanswered_inbound_call", new=AsyncMock(return_value=True)), \
         patch(f"{_RINGBACK}.dialer_extra_answer.continue_after_ringback", new=AsyncMock(return_value={"action": action})), \
         patch(f"{_RINGBACK}.cancel_call", new=AsyncMock()) as cancel, \
         patch(f"{_RINGBACK}.record_voicemail_playback_failure", new=AsyncMock()) as failed, \
         patch(f"{_RINGBACK}.call_stages.record_call_stage", new=AsyncMock()):
        await dialer_voicemail_ringback.expire_ringback(parent_call_sid=_PARENT, prospect_call_sid=_SID, delay_seconds=0)
    cancel.assert_not_awaited()   # the apology update ends the call itself
    failed.assert_awaited_once_with(parent_call_sid=_PARENT, reason="person_answered")


@pytest.mark.asyncio
async def test_ringback_continuation_starts_the_chain_after_the_ringback_step():
    with patch(f"{_XA}.begin", new=AsyncMock(return_value={"status": "active"})) as begin, \
         patch(f"{_TXA}.continue_after", new=AsyncMock(return_value={"action": "teammate"})) as cont:
        await dialer_extra_answer.continue_after_ringback(parent_call_sid=_PARENT, prospect_call_sid=_SID)
    assert begin.await_args.kwargs == {"provider": "twilio", "prospect_call_id": _SID,
                                       "source": "ringback", "parent_call_sid": _PARENT}
    cont.assert_awaited_once_with(_SID, after="ringback")


@pytest.mark.asyncio
async def test_ringback_offer_waits_with_the_hello_clip_not_music():
    with patch(f"{_RINGBACK}.call_stages.offer_voicemail_ringback", new=AsyncMock(
            return_value={"inbound_call_id": "ic-1"})), patch(f"{_RINGBACK}.notify_inbound_call"):
        twiml = await dialer_voicemail_ringback.offer_ringback_twiml(
            parent_call_sid=_PARENT, prospect_call_sid=_SID, endpoint_root="https://api.example.com/t")
    assert f'waitUrl="https://api.example.com/t/dialer-extra-answer-wait?call={_SID}"' in twiml
    assert call_stages.RINGBACK_OFFER_SECONDS == 10


# ── Twilio: the agent's transfer-back window ────────────────────────────────

@pytest.mark.asyncio
async def test_transfer_back_offer_rings_whoever_is_free():
    with patch(f"{_XA}.reserve_transfer", new=AsyncMock(return_value={"status": "offered", "inbound_call_id": "ic-9"})), \
         patch(f"{_TXA}.notify_inbound_call") as notify, patch(f"{_TXA}.hand_over", new=AsyncMock()) as hand:
        assert await dialer_extra_answer.drive_agent({"prospect_call_id": _SID}) == "offered"
    notify.assert_called_once_with("ic-9")
    hand.assert_not_awaited()


@pytest.mark.asyncio
async def test_joined_transfer_target_triggers_the_handover():
    with patch(f"{_XA}.reserve_transfer", new=AsyncMock(return_value={"status": "joined"})), \
         patch(f"{_TXA}.hand_over", new=AsyncMock(return_value=True)) as hand:
        await dialer_extra_answer.drive_agent({"prospect_call_id": _SID})
    hand.assert_awaited_once_with(_SID)


@pytest.mark.asyncio
async def test_handover_publishes_once_to_the_agent_run():
    with patch(f"{_XA}.claim_transfer", new=AsyncMock(return_value={
             "workflow_run_id": "501", "conference_name": f"call-{_SID}", "user_name": "Max T", "user_id": _REP})), \
         patch.object(live_control, "publish_command", new=AsyncMock(return_value=1)) as publish, \
         patch(f"{_XA}.release_transfer", new=AsyncMock()) as release:
        assert await dialer_extra_answer.hand_over(_SID) is True
    publish.assert_awaited_once_with("501", {
        "type": "dialer_transfer", "conference_name": f"call-{_SID}", "to_name": "Max T", "to_user_id": _REP})
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_handover_nobody_listening_is_released_for_a_retry():
    with patch(f"{_XA}.claim_transfer", new=AsyncMock(return_value={"workflow_run_id": "501", "conference_name": "c"})), \
         patch.object(live_control, "publish_command", new=AsyncMock(return_value=0)), \
         patch(f"{_XA}.release_transfer", new=AsyncMock()) as release:
        assert await dialer_extra_answer.hand_over(_SID) is False
    release.assert_awaited_once_with(_SID)


@pytest.mark.asyncio
async def test_handover_not_claimable_does_nothing():
    with patch(f"{_XA}.claim_transfer", new=AsyncMock(return_value=None)), \
         patch.object(live_control, "publish_command", new=AsyncMock()) as publish:
        assert await dialer_extra_answer.hand_over(_SID) is False
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_window_over_asks_the_agent_to_book_the_callback():
    state = {"prospect_call_id": _SID, "workflow_run_id": "501", "callback_in_chain": True, "rep_name": "Max Tan"}
    with patch(f"{_XA}.reserve_transfer", new=AsyncMock(return_value={"status": "deadline"})), \
         patch.object(live_control, "publish_command", new=AsyncMock(return_value=1)) as publish, \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        await dialer_extra_answer.drive_agent(state)
    command = publish.await_args.args[1]
    assert command["type"] == "whisper" and "Max" in command["text"] and "call them back" in command["text"]
    mark.assert_awaited_once_with("twilio", _SID, "callback_requested")


@pytest.mark.asyncio
async def test_window_over_without_the_callback_step_only_stops_trying():
    state = {"prospect_call_id": _SID, "workflow_run_id": "501", "callback_in_chain": False}
    with patch(f"{_XA}.reserve_transfer", new=AsyncMock(return_value={"status": "deadline"})), \
         patch.object(live_control, "publish_command", new=AsyncMock()) as publish, \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        await dialer_extra_answer.drive_agent(state)
    publish.assert_not_awaited()
    mark.assert_awaited_once_with("twilio", _SID, "callback_requested")


@pytest.mark.asyncio
async def test_window_over_is_not_repeated_once_requested():
    state = {"prospect_call_id": _SID, "callback_requested_at": "t", "callback_in_chain": True, "workflow_run_id": "1"}
    with patch(f"{_XA}.reserve_transfer", new=AsyncMock(return_value={"status": "deadline"})), \
         patch.object(live_control, "publish_command", new=AsyncMock()) as publish, \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        await dialer_extra_answer.drive_agent(state)
    publish.assert_not_awaited()
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_target_joining_starts_the_handover_at_once():
    with patch(f"{_XA}.record_rep_joined", new=AsyncMock(return_value={"transfer": True, "prospect_call_id": _SID})), \
         patch(f"{_TXA}.hand_over", new=AsyncMock()) as hand:
        await dialer_extra_answer.on_rep_joined(conference_name=f"call-{_SID}", rep_user_id=_REP)
    hand.assert_awaited_once_with(_SID)
    with patch(f"{_XA}.record_rep_joined", new=AsyncMock(return_value={"transfer": False})), \
         patch(f"{_TXA}.hand_over", new=AsyncMock()) as hand:
        await dialer_extra_answer.on_rep_joined(conference_name=f"call-{_SID}", rep_user_id=_MATE)
    hand.assert_not_awaited()


# ── live_control: the pipeline side of the handover ─────────────────────────

def test_the_portal_cannot_send_a_dialer_transfer():
    with pytest.raises(live_control.SupervisorCommandError):
        live_control.validate_command({"type": "dialer_transfer", "conference_name": f"call-{_SID}"})


def test_conversation_summary_keeps_the_last_spoken_turns_only():
    messages = [{"role": "system", "content": "prompt"}]
    messages += [{"role": "user", "content": f"line {i}"} for i in range(8)]
    messages += [{"role": "user", "content": "[Private instruction from your supervisor.] x"},
                 {"role": "assistant", "content": [{"type": "text", "text": "Sure"}]}]
    summary = live_control.conversation_summary(messages)
    assert summary.splitlines()[-1] == "Agent: Sure"
    assert "prompt" not in summary and "Private" not in summary
    assert len(summary.splitlines()) == 6


def _transfer_run(call_id=_SID, extra=True):
    return SimpleNamespace(
        initial_context={"dialer_extra_answer_id": "x" if extra else None, "dialer_original_call_id": call_id},
        gathered_context={"call_id": call_id},
    )


@pytest.mark.asyncio
async def test_agent_hands_over_into_its_own_calls_room():
    engine = MagicMock()
    engine.task.queue_frame = AsyncMock()
    engine.end_call_with_reason = AsyncMock()
    engine.context.get_messages.return_value = [{"role": "user", "content": "send me pricing"}]
    manager = MagicMock(store_transfer_context=AsyncMock())
    fake_db = MagicMock(get_workflow_run_by_id=AsyncMock(return_value=_transfer_run()))
    with patch("api.db.db_client", fake_db), \
         patch("api.services.telephony.call_transfer_manager.get_call_transfer_manager", new=AsyncMock(return_value=manager)), \
         patch(f"{_XA}.set_summary", new=AsyncMock(return_value=True)) as summary, \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        await live_control.SupervisorControl(engine, 501, 1).dialer_transfer(
            live_control.dialer_transfer_command(conference_name=f"call-{_SID}", to_name="Max Tan", to_user_id=_REP))
    context = manager.store_transfer_context.await_args.args[0]
    assert context.original_call_sid == _SID and context.conference_name == f"call-{_SID}"
    spoken = engine.task.queue_frame.await_args.args[0]
    assert "Max is free now" in spoken.text
    engine.end_call_with_reason.assert_awaited_once_with("transfer_call", abort_immediately=False)
    summary.assert_awaited_once_with(_SID, "Them: send me pricing")
    mark.assert_awaited_once_with("twilio", _SID, "agent_transferred", {"user_id": _REP})


@pytest.mark.asyncio
@pytest.mark.parametrize("conference,run", [
    ("call-CA" + "5" * 32, _transfer_run()),          # another call's room
    (f"call-{_SID}", _transfer_run(extra=False)),    # not an extra-answer run
    (f"call-{_SID}", None),
])
async def test_agent_refuses_a_handover_that_is_not_its_own(conference, run):
    engine = MagicMock()
    engine.end_call_with_reason = AsyncMock()
    fake_db = MagicMock(get_workflow_run_by_id=AsyncMock(return_value=run))
    with patch("api.db.db_client", fake_db), patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        await live_control.SupervisorControl(engine, 501, 1).dialer_transfer(
            {"type": "dialer_transfer", "conference_name": conference, "to_user_id": _REP})
    engine.end_call_with_reason.assert_not_awaited()
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_routes_the_internal_transfer_command():
    control = live_control.SupervisorControl(MagicMock(), 501, 1)
    with patch.object(control, "dialer_transfer", new=AsyncMock()) as transfer, \
         patch.object(control, "take_over", new=AsyncMock()) as takeover:
        await control._handle({"type": "dialer_transfer", "conference_name": "c"})
    transfer.assert_awaited_once()
    takeover.assert_not_awaited()


# ── agent start opens the window ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_media_start_opens_the_transfer_window_for_extra_answers():
    from api.services.telephony.providers.twilio import dialer_agent_handoff as handoff

    run = SimpleNamespace(initial_context={"dialer_original_call_id": _SID, "dialer_extra_answer_id": "x"},
                          gathered_context={"dialer_media_authenticated": True})
    with patch.object(handoff.db_client, "get_workflow_run", new=AsyncMock(return_value=run)), \
         patch.object(handoff, "_rpc", new=AsyncMock(return_value=True)), \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        assert await handoff.record_handoff_event(call_id=_SID, event="started", event_at="t", workflow_run_id="7")
    mark.assert_awaited_once_with("twilio", _SID, "agent_started")
    run.initial_context.pop("dialer_extra_answer_id")
    with patch.object(handoff.db_client, "get_workflow_run", new=AsyncMock(return_value=run)), \
         patch.object(handoff, "_rpc", new=AsyncMock(return_value=True)), \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark:
        await handoff.record_handoff_event(call_id=_SID, event="started", event_at="t", workflow_run_id="7")
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_extra_answer_agent_plan_must_match_the_reserved_call():
    from api.services.telephony.providers.twilio import dialer_agent_handoff as handoff

    with patch(f"{_XA}.prepare_agent_plan", new=AsyncMock(return_value={
            "call_id": "CA" + "8" * 32, "request_id": "a", "extra_answer_id": "a"})):
        with pytest.raises(handoff.AgentHandoffUnavailable):
            await handoff.prepare_extra_answer_agent(prospect_call_sid=_SID, account_sid=_ACCOUNT)
    with patch(f"{_XA}.prepare_agent_plan", new=AsyncMock(return_value=None)):
        assert await handoff.prepare_extra_answer_agent(prospect_call_sid=_SID, account_sid=_ACCOUNT) is None
    plan = {"call_id": _SID, "request_id": "a", "extra_answer_id": "a", "parent_call_sid": _PARENT}
    prepared = handoff.PreparedAgentHandoff(9, "<Response/>")
    with patch(f"{_XA}.prepare_agent_plan", new=AsyncMock(return_value=plan)), \
         patch.object(handoff, "_materialize_handoff", new=AsyncMock(return_value=prepared)) as build:
        assert await handoff.prepare_extra_answer_agent(prospect_call_sid=_SID, account_sid=_ACCOUNT, disclose=False) is prepared
    assert build.await_args.kwargs["extra_context"] == {"dialer_extra_answer_id": "a"}
    assert build.await_args.kwargs["disclose"] is False
    assert build.await_args.kwargs["parent_call_sid"] == _PARENT


# ── SignalWire ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signalwire_moves_a_live_call_to_the_agent_number():
    with patch(f"{_XA}.advance", new=AsyncMock(return_value={
             "action": "agent", "prospect_call_id": "sw-1", "rep_user_id": _REP})), \
         patch(f"{_SXA}._agent_number", new=AsyncMock(return_value="+15555550100")), \
         patch(f"{_XA}.mark", new=AsyncMock()) as mark, \
         patch("api.services.telephony.dialer.signalwire_calling.update_call_swml", new=AsyncMock()) as update:
        await signalwire_extra_answer.continue_after("sw-1", after="teammate")
    mark.assert_awaited_once_with("signalwire", "sw-1", "agent_started")
    swml = update.await_args.kwargs["swml"]
    assert {"connect": {"to": "+15555550100", "timeout": 30, "max_duration": 7200}} in swml["sections"]["main"]


@pytest.mark.asyncio
async def test_signalwire_without_an_agent_number_skips_to_the_apology():
    advance = AsyncMock(side_effect=[
        {"action": "agent", "prospect_call_id": "sw-1", "rep_user_id": _REP},
        {"action": "apology", "prospect_call_id": "sw-1", "rep_user_id": _REP},
    ])
    with patch(f"{_XA}.advance", new=advance), \
         patch(f"{_SXA}._agent_number", new=AsyncMock(return_value="")), \
         patch(f"{_XA}.clip_url", new=AsyncMock(return_value="https://storage.example.com/sorry.wav")), \
         patch("api.services.telephony.dialer.signalwire_calling.update_call_swml", new=AsyncMock()) as update:
        await signalwire_extra_answer.continue_after("sw-1", after="teammate")
    assert advance.await_args_list[1].kwargs == {"provider": "signalwire", "prospect_call_id": "sw-1", "after": "agent"}
    assert update.await_args.kwargs["swml"] == {"sections": {"main": [
        {"play": {"url": "https://storage.example.com/sorry.wav"}}, {"hangup": {}}]}}


@pytest.mark.asyncio
async def test_signalwire_ringback_teammate_reuses_the_waiting_room():
    spawned, spawn = _closing_spawn()
    with patch(f"{_XA}.begin", new=AsyncMock(return_value={"status": "active"})) as begin, \
         patch(f"{_XA}.advance", new=AsyncMock(return_value={
             "action": "teammate", "prospect_call_id": "sw-1", "inbound_call_id": "ic-3"})), \
         patch(f"{_SXA}.notify_inbound_call") as notify, patch(f"{_SXA}._spawn", side_effect=spawn), \
         patch("api.services.telephony.dialer.signalwire_calling.update_call_swml", new=AsyncMock()) as update:
        await signalwire_extra_answer.continue_after_ringback(rep_call_id="browser-call", provider_call_id="sw-1")
    assert begin.await_args.kwargs["source"] == "ringback"
    notify.assert_called_once_with("ic-3")
    assert spawned == ["expire_offer_later"]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalwire_teammate_room_plays_hello_then_joins():
    with patch(f"{_XA}.clip_url", new=AsyncMock(return_value=None)):
        doc = await signalwire_extra_answer.teammate_swml(
            rep_user_id=_REP, conference_name="sysevo-overflow-1", status_callback_url=None, disclose=False)
    assert doc == {"sections": {"main": [
        {"play": {"url": "say:Hi, one moment."}},
        {"join_conference": {"name": "sysevo-overflow-1", "start_on_enter": False, "end_on_exit": True}},
    ]}}
    with pytest.raises(ValueError):
        await signalwire_extra_answer.teammate_swml(
            rep_user_id=_REP, conference_name="sysevo-par-1", status_callback_url=None, disclose=False)


@pytest.mark.asyncio
async def test_signalwire_retried_decision_never_reserves_a_second_step():
    with patch(f"{_XA}.begin", new=AsyncMock(return_value={"status": "active", "step": "teammate"})), \
         patch(f"{_XA}.advance", new=AsyncMock()) as advance:
        doc = await signalwire_extra_answer.first_answer_swml(
            session_id="s", leg_id="l", provider_call_id="sw-1", rep_call_id="r")
    advance.assert_not_awaited()
    assert doc == {"sections": {"main": [{"hangup": {}}]}}


# ── The reconciliation worker ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_dispatches_each_job_and_always_releases_its_lease():
    from api.tasks import dialer_overflow_reconciliation as worker

    jobs = [
        {"id": "a", "provider": "twilio", "work": "offer_expired", "prospect_call_id": _SID, "account_sid": _ACCOUNT},
        {"id": "b", "provider": "twilio", "work": "agent", "prospect_call_id": _SID},
        {"id": "c", "provider": "signalwire", "work": "offer_expired", "prospect_call_id": "sw-1"},
    ]
    with patch(f"{_XA}.claim_work", new=AsyncMock(return_value=jobs)), \
         patch(f"{_XA}.release_lease", new=AsyncMock()) as release, \
         patch(f"{_TXA}.continue_after", new=AsyncMock()) as twilio_next, \
         patch(f"{_TXA}.drive_agent", new=AsyncMock(side_effect=RuntimeError("boom"))) as agent, \
         patch(f"{_SXA}.continue_after", new=AsyncMock()) as sw_next:
        await worker.reconcile_extra_answers()
    twilio_next.assert_awaited_once_with(_SID, after="teammate", account_sid=_ACCOUNT)
    agent.assert_awaited_once_with(jobs[1])
    sw_next.assert_awaited_once_with("sw-1", after="teammate")
    assert sorted(c.args[0] for c in release.await_args_list) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_worker_tick_runs_old_recovery_and_the_chain():
    from api.tasks import dialer_overflow_reconciliation as worker

    with patch.object(worker, "SUPABASE_URL", "https://db.example.com"), \
         patch.object(worker, "SUPABASE_SERVICE_ROLE_KEY", "k"), \
         patch.object(worker, "recover_overflow_offers", new=AsyncMock(side_effect=RuntimeError("x"))), \
         patch.object(worker, "reconcile_extra_answers", new=AsyncMock()) as chain:
        await worker.reconcile_dialer_overflow({})
    chain.assert_awaited_once()


def test_wait_endpoint_is_signed_and_plays_the_hello():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.services.telephony.providers.twilio import routes

    app = FastAPI()
    app.include_router(routes.router)
    with patch.object(routes, "_verify_twilio_signature", new=AsyncMock(return_value=False)):
        assert TestClient(app).post(f"/dialer-extra-answer-wait?call={_SID}", data={}).status_code == 401
    with patch.object(routes, "_verify_twilio_signature", new=AsyncMock(return_value=True)), \
         patch(f"{_TXA}.hello_wait_twiml", new=AsyncMock(return_value="<Response/>")) as hello:
        response = TestClient(app).post(f"/dialer-extra-answer-wait?call={_SID}", data={"CallSid": "CA" + "7" * 32},
                                        headers={"X-Twilio-Signature": "sig"})
    assert response.text == "<Response/>"
    hello.assert_awaited_once_with(_SID)


def test_modules_import_without_cycles():
    assert "api.services.telephony.dialer.signalwire_extra_answer" in sys.modules
