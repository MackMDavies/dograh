"""SignalWire parallel dialing: end-to-end contract fixes (2026-10-19 audit)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.dialer import signalwire_routes as routes
from api.services.telephony.dialer.swml import (
    SIGNALWIRE_AMD_PRESETS,
    build_signalwire_parallel_leg_swml,
)

MODULE = "api.services.telephony.dialer.signalwire_routes"
CALLING = "api.services.telephony.dialer.signalwire_calling"
SECRET = "audit-secret"
SESSION = "00000000-0000-0000-0000-0000000000a1"
LEG = "00000000-0000-0000-0000-0000000000b1"
OTHER_LEG = "00000000-0000-0000-0000-0000000000b2"
CONFERENCE = f"sysevo-par-{SESSION}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", SECRET)


def _request(body, query):
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    request.query_params = {"k": SECRET, **query}
    return request


def _nonce_query(session_id=SESSION, leg_id=LEG, **extra):
    return {"session_id": session_id, "leg_id": leg_id,
            "call_id": f"sw-parallel:{session_id}:{leg_id}", **extra}


def _body(response):
    return json.loads(response.body)


def _is_hangup(document):
    return document == {"sections": {"main": [{"hangup": {}}]}}


# --- AMD: never bridge a greeting, play the drop after the beep -------------

@pytest.mark.parametrize("preset", ["fastest", "fast", "balanced", "accurate"])
def test_amd_waits_for_the_beep_instead_of_timing_out_mid_greeting(preset):
    steps = build_signalwire_parallel_leg_swml(
        decision_url="https://api.example.test/decide", amd_preset=preset,
    )["sections"]["main"]
    detect = steps[0]["detect_machine"]
    assert detect["detect_message_end"] is True and detect["wait"] is True
    # The old 1.2-3.2 s overall cap ended detection during a typical greeting.
    assert detect["timeout"] == 30.0
    threshold, end_silence, initial = SIGNALWIRE_AMD_PRESETS[preset]
    assert (detect["machine_voice_threshold"], detect["end_silence_timeout"],
            detect["initial_timeout"]) == (threshold, end_silence, initial)
    params = steps[1]["execute"]["params"]
    assert params == {"amd_result": "${detect_result}", "amd_ms": "${detect_ms}",
                      "beep": "${detect_machine_beep}"}


def test_unknown_preset_falls_back_to_signalwire_defaults():
    detect = build_signalwire_parallel_leg_swml(
        decision_url="https://api.example.test/decide", amd_preset="bogus",
    )["sections"]["main"][0]["detect_machine"]
    assert detect["machine_voice_threshold"] == 1.25
    assert detect["initial_timeout"] == 4.5


async def test_leg_bootstrap_passes_account_preset_to_amd():
    context = {
        "session": {"status": "dialing", "rep_user_id": "rep", "rep_call_id": "browser-call", "line_count": 3},
        "leg": {"provider_call_id": "prospect-call-1", "entry_id": "entry", "status": "screening"},
    }
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)),
        patch(f"{MODULE}.signalwire_parallel_store.get_voicemail_policy",
              new=AsyncMock(return_value={"machine_detection_preset": "accurate"})),
        patch(f"{MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", ""))),
    ):
        response = await routes.handle_sw_parallel_leg(
            _request({"call": {"call_id": "prospect-call-1"}}, _nonce_query())
        )
    detect = _body(response)["sections"]["main"][0]["detect_machine"]
    assert detect["machine_voice_threshold"] == SIGNALWIRE_AMD_PRESETS["accurate"][0]


# --- Signed nonce binds the session and leg ----------------------------------

async def test_callback_cannot_be_replayed_for_another_leg():
    query = _nonce_query()
    query["leg_id"] = OTHER_LEG  # signature still names LEG
    context = AsyncMock()
    with patch(f"{MODULE}.signalwire_parallel_store.get_leg_context", new=context):
        decision = await routes.handle_sw_parallel_decision(
            _request({"call": {"call_id": "prospect-call-1"}, "params": {"amd_result": "human"}}, query)
        )
        status = await routes.handle_sw_parallel_status(
            _request({"params": {"call_id": "prospect-call-1", "call_state": "ended"}}, query)
        )
    assert _is_hangup(_body(decision))
    assert status.status_code == 400
    context.assert_not_awaited()


# --- Batch completion --------------------------------------------------------

async def test_abandoned_early_sibling_counts_as_settled():
    """A loser cancelled within six seconds used to keep the session open for ever."""
    legs = [
        {"id": LEG, "status": "completed"},
        {"id": OTHER_LEG, "status": "abandoned_early"},
    ]
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=legs)),
        patch(f"{MODULE}.signalwire_parallel_store.finish_session", new=AsyncMock(return_value=True)) as finish,
        patch(f"{CALLING}.end_call", new=AsyncMock()) as end,
    ):
        done = await routes._finish_signalwire_batch_if_settled(
            session_id=SESSION, session={"winner_leg_id": LEG, "rep_call_id": "browser-call"},
        )
    assert done is True
    finish.assert_awaited_once_with(session_id=SESSION, status="completed")
    end.assert_awaited_once_with(call_id="browser-call", reason="hangup")


async def test_unsettled_batch_is_left_open():
    legs = [{"id": LEG, "status": "connected"}, {"id": OTHER_LEG, "status": "abandoned_early"}]
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=legs)),
        patch(f"{MODULE}.signalwire_parallel_store.finish_session", new=AsyncMock()) as finish,
    ):
        assert not await routes._finish_signalwire_batch_if_settled(session_id=SESSION, session={})
    finish.assert_not_awaited()


async def test_ended_event_for_cancelled_loser_keeps_its_no_answer_history():
    context = {
        "session": {"status": "connected", "rep_user_id": "rep", "rep_call_id": "browser-call", "winner_leg_id": OTHER_LEG},
        "leg": {"id": LEG, "provider_call_id": "prospect-call-1", "status": "abandoned_early"},
    }
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)),
        patch(f"{MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=False)),
        patch(f"{MODULE}.close_inbound_call", new=AsyncMock()),
        patch(f"{MODULE}.evaluate_overflow_guard", new=AsyncMock()),
        patch(f"{MODULE}.update_dialer_call_status", new=AsyncMock()) as history,
        patch(f"{MODULE}._finish_signalwire_batch_if_settled", new=AsyncMock(return_value=True)) as finish,
    ):
        response = await routes.handle_sw_parallel_status(_request(
            {"params": {"call_id": "prospect-call-1", "call_state": "ended", "end_reason": "cancel"}},
            _nonce_query(),
        ))
    assert _body(response)["reason"] == "stale_state"
    history.assert_not_awaited()
    finish.assert_awaited_once()


# --- Rep hangs up while lines are ringing -----------------------------------

def _leave(call_id="browser-call", name=CONFERENCE):
    return {"timestamp": 1_800_000_000.0,
            "params": {"status": "participant-leave", "call_id": call_id, "name": name}}


async def test_rep_leaving_a_dialing_batch_cancels_it_and_its_ringing_lines():
    session = {"status": "dialing", "rep_call_id": "browser-call", "conference_name": CONFERENCE,
               "rep_user_id": "rep", "winner_leg_id": None}
    legs = [
        {"id": LEG, "status": "ringing", "provider_call_id": "prospect-call-1"},
        {"id": OTHER_LEG, "status": "queued", "provider_call_id": "prospect-call-2"},
    ]
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_session", new=AsyncMock(return_value=session)),
        patch(f"{MODULE}.signalwire_parallel_store.finish_session", new=AsyncMock(return_value=True)) as finish,
        patch(f"{MODULE}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=legs)),
        patch(f"{MODULE}.signalwire_parallel_store.claim_ringing_cancel", new=AsyncMock(return_value=True)),
        patch(f"{MODULE}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)),
        patch(f"{MODULE}.update_dialer_call_status", new=AsyncMock()),
        patch(f"{CALLING}.end_call", new=AsyncMock()) as end,
    ):
        response = await routes.handle_sw_parallel_conference(
            _request(_leave(), _nonce_query(leg_id="rep"))
        )
    assert _body(response) == {"ok": True}
    finish.assert_awaited_once_with(session_id=SESSION, status="cancelled")
    assert {call.kwargs["call_id"] for call in end.await_args_list} == {"prospect-call-1", "prospect-call-2"}


async def test_rep_leave_after_a_winner_does_not_cancel_the_session():
    session = {"status": "connected", "rep_call_id": "browser-call", "conference_name": CONFERENCE,
               "rep_user_id": "rep", "winner_leg_id": LEG}
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_session", new=AsyncMock(return_value=session)),
        patch(f"{MODULE}.signalwire_parallel_store.finish_session", new=AsyncMock()) as finish,
        patch(f"{MODULE}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=[])),
    ):
        await routes.handle_sw_parallel_conference(_request(_leave(), _nonce_query(leg_id="rep")))
    finish.assert_not_awaited()


@pytest.mark.parametrize("call_id,name", [("someone-else", CONFERENCE), ("browser-call", "sysevo-par-other")])
async def test_leave_of_another_participant_or_room_is_refused(call_id, name):
    session = {"status": "dialing", "rep_call_id": "browser-call", "conference_name": CONFERENCE}
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_session", new=AsyncMock(return_value=session)),
        patch(f"{MODULE}._abandon_signalwire_batch_after_rep_left", new=AsyncMock()) as abandon,
    ):
        response = await routes.handle_sw_parallel_conference(
            _request(_leave(call_id, name), _nonce_query(leg_id="rep"))
        )
    assert response.status_code == 409
    abandon.assert_not_awaited()


# --- Lost play callback ------------------------------------------------------

async def test_ended_voicemail_leg_schedules_settlement():
    context = {"session": {"status": "dialing", "rep_user_id": "rep"},
               "leg": {"id": LEG, "provider_call_id": "prospect-call-1", "status": "voicemail_playing"}}
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)),
        patch(f"{MODULE}._spawn_background") as spawn,
        patch(f"{MODULE}._settle_unreported_voicemail_playback", new=MagicMock(return_value="coro")) as settle,
    ):
        response = await routes.handle_sw_parallel_status(_request(
            {"params": {"call_id": "prospect-call-1", "call_state": "ended", "end_reason": "hangup"}},
            _nonce_query(),
        ))
    assert _body(response)["awaiting_playback_result"] is True
    spawn.assert_called_once_with("coro")
    assert settle.call_args.kwargs["provider_call_id"] == "prospect-call-1"


@pytest.mark.parametrize("status,settles", [("voicemail_playing", True), ("voicemail", False)])
async def test_unreported_playback_is_settled_only_if_still_playing(status, settles):
    context = {"session": {"rep_user_id": "rep", "rep_call_id": "browser-call"},
               "leg": {"id": LEG, "status": status}}
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)),
        patch(f"{MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)) as update,
        patch(f"{MODULE}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)) as outcome,
        patch(f"{MODULE}.update_dialer_call_status", new=AsyncMock()),
        patch(f"{MODULE}._finish_signalwire_batch_if_settled", new=AsyncMock()) as finish,
    ):
        await routes._settle_unreported_voicemail_playback(
            session_id=SESSION, leg_id=LEG, provider_call_id="prospect-call-1",
            session=context["session"], delay_seconds=0,
        )
    assert update.await_count == outcome.await_count == finish.await_count == (1 if settles else 0)
    if settles:
        assert update.await_args.kwargs["status"] == "voicemail"
        assert outcome.await_args.kwargs["outcome"] == "voicemail"


# --- Batch start ---------------------------------------------------------------

def _start_patches(entries, compliance, expire=None):
    rep_id = "00000000-0000-0000-0000-0000000000c1"
    caller_ids = {entry["id"]: "+14155550999" for entry in entries}
    return rep_id, (
        patch("api.services.telephony.dialer.parallel_auth.verify_parallel_auth_token", return_value=rep_id),
        patch(f"{MODULE}._rep_supabase_id", new=AsyncMock(return_value=rep_id)),
        patch(f"{MODULE}._resolve_signalwire_caller_id", new=AsyncMock(return_value="+14155550999")),
        patch(f"{MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", ""))),
        patch(f"{MODULE}._load_signalwire_parallel_entries", new=AsyncMock(return_value=entries)),
        patch(f"{MODULE}.signalwire_parallel_store.expire_stale_sessions",
              new=expire or AsyncMock(return_value=0)),
        patch(f"{MODULE}.signalwire_parallel_store.start_session",
              new=AsyncMock(return_value=[{"id": f"leg-{i}"} for i in range(len(entries))])),
        patch(f"{MODULE}._resolve_signalwire_parallel_caller_ids", new=AsyncMock(return_value=caller_ids)),
        patch(f"{MODULE}._check_signalwire_parallel_compliance", new=compliance),
        patch(f"{MODULE}.callable_batch_entries", new=AsyncMock(side_effect=lambda items: ([entry for entry, _phone in items], {}))),
        patch(f"{MODULE}.signalwire_parallel_store.finish_session", new=AsyncMock(return_value=True)),
        patch(f"{MODULE}.create_dialer_call", new=AsyncMock(return_value=True)),
        patch(f"{CALLING}.originate_call", new=AsyncMock(side_effect=lambda **kw: {"id": "call-" + kw["to_number"]})),
        patch(f"{MODULE}.signalwire_parallel_store.register_provider_call", new=AsyncMock(return_value=True)),
    )


def _entries(count):
    return [{"id": f"00000000-0000-0000-0000-00000000d{i:03d}", "phone": f"+1415555{i:04d}"} for i in range(count)]


async def _start(rep_id, entries, patches):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        return await routes._start_signalwire_session(
            request=_request({}, {}), payload={},
            query={"ParallelAuthToken": "t", "ParallelEntryIds": json.dumps([e["id"] for e in entries]),
                   "ParallelLineCount": str(len(entries)), "CallSid": "browser-call-0009"},
            identity=rep_id,
        )


async def test_compliance_checks_run_concurrently_for_every_line():
    entries = _entries(10)
    started = []
    barrier = asyncio.Event()

    async def check(**kwargs):
        started.append(kwargs["phone"])
        if len(started) == len(entries):
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=1)

    rep_id, patches = _start_patches(entries, AsyncMock(side_effect=check))
    response = await _start(rep_id, entries, patches)
    assert len(started) == 10
    assert "join_conference" in _body(response)["sections"]["main"][0]


async def test_one_blocked_destination_still_fails_the_whole_batch_closed():
    entries = _entries(3)

    async def check(**kwargs):
        if kwargs["phone"] == entries[1]["phone"]:
            raise RuntimeError("Dial blocked by server suppression check: dnc")

    rep_id, patches = _start_patches(entries, AsyncMock(side_effect=check))
    response = await _start(rep_id, entries, patches)
    # Still fails closed (nothing dials), but the rep now hears why before the hang-up.
    main = _body(response)["sections"]["main"]
    assert main[-1] == {"hangup": {}} and main[0]["play"]["url"].startswith("say:")


async def test_stale_sessions_are_expired_before_reserving():
    entries = _entries(2)
    expire = AsyncMock(return_value=1)
    rep_id, patches = _start_patches(entries, AsyncMock(), expire=expire)
    await _start(rep_id, entries, patches)
    expire.assert_awaited_once_with(rep_user_id=rep_id)


async def test_expire_stale_sessions_never_blocks_a_dial():
    from api.services.telephony.dialer import signalwire_parallel_store as store

    with patch.object(store, "_rpc", new=AsyncMock(side_effect=store.SignalWireParallelStoreError("404"))):
        assert await store.expire_stale_sessions(rep_user_id="rep") == 0
    with patch.object(store, "_rpc", new=AsyncMock(return_value=2)) as rpc:
        assert await store.expire_stale_sessions(rep_user_id="rep") == 2
    rpc.assert_awaited_once_with("dialer_signalwire_parallel_expire_stale", {"p_rep_user_id": "rep"})


# --- Fresh rep proof -----------------------------------------------------------

async def test_parallel_auth_endpoint_issues_a_verifiable_fresh_token(monkeypatch):
    from api.services.telephony.dialer.parallel_auth import verify_parallel_auth_token

    monkeypatch.setenv("SIGNALWIRE_PARALLEL_AUTH_SECRET", "proof-secret")
    user = MagicMock(provider_id="00000000-0000-0000-0000-0000000000e1")
    body = await routes.get_signalwire_parallel_auth_token(user=user)
    assert verify_parallel_auth_token(body["parallel_auth_token"]) == user.provider_id


async def test_parallel_auth_endpoint_reports_missing_secret(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.delenv("SIGNALWIRE_PARALLEL_AUTH_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    with pytest.raises(HTTPException) as raised:
        await routes.get_signalwire_parallel_auth_token(
            user=MagicMock(provider_id="00000000-0000-0000-0000-0000000000e1")
        )
    assert raised.value.status_code == 503
