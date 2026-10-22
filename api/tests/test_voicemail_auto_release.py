"""Background voicemail drops: the rep is released the moment playback is claimed.

Covers both carriers the sales floor uses (Twilio, SignalWire):

* the rep's "Send my voicemail automatically" choice (on / off / unset -> team
  default) is resolved on the server for the call's own rep;
* with it on, a single call's mailbox gets the recording and the rep's leg is
  ended straight away while the prospect leg keeps playing;
* with it off, the mailbox is passed to the rep, whose one tap plays it the same
  way and releases them;
* every machine line in a parallel batch is dropped regardless of that choice,
  and never bridged;
* conference teardown, retried joins and late callbacks never cut a message off
  or play it twice.

Every provider and database boundary is mocked; no call is placed.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.providers.twilio import routes as twilio_routes
from api.services.telephony.providers.twilio.dialer_call_log import effective_auto_send
from api.services.telephony.providers.twilio.routes import router

_PATH = "api.services.telephony.providers.twilio.routes"
_SW = "api.services.telephony.dialer.signalwire_routes"
_PARENT = "CA" + "abcdef0123456789" * 2
_CHILD = "CA" + "0123456789abcdef" * 2
_REP = "11111111-1111-1111-1111-111111111111"
_LEG = "11111111-1111-4111-8111-111111111111"
_ENTRY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_DROP = "33333333-3333-4333-8333-333333333333"
_URL = "https://db.example.com/storage/v1/object/sign/voicemail-drops/u/message.wav?token=x"


@pytest.fixture(autouse=True)
def _signed(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setattr(f"{_PATH}.close_floor_monitor_for_provider", AsyncMock(return_value=False))
    with patch(f"{_PATH}.RequestValidator.validate", return_value=True), \
         patch(f"{_PATH}._resolve_dialer_auth_token", new=AsyncMock(return_value="test-auth-token")):
        yield


def _app(authenticated: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if authenticated:
        from types import SimpleNamespace
        app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(provider_id=_REP)
    return TestClient(app)


# ── per-rep preference resolution ───────────────────────────────────────────

@pytest.mark.parametrize(("policy", "single", "parallel"), [
    # rep unset: the team default decides the single call
    ({"auto_drop_enabled": True, "single_auto_send": True, "parallel_auto_send": True}, True, True),
    ({"auto_drop_enabled": False, "single_auto_send": False, "parallel_auto_send": True}, False, True),
    # rep explicitly OFF under a team default of ON: single asks, parallel still drops
    ({"auto_drop_enabled": True, "rep_auto_send": False, "single_auto_send": False, "parallel_auto_send": True}, False, True),
    # rep explicitly ON under a team default of OFF
    ({"auto_drop_enabled": False, "rep_auto_send": True, "single_auto_send": True, "parallel_auto_send": True}, True, True),
    # manager chose Hang up: nothing plays anywhere
    ({"auto_drop_enabled": True, "rep_auto_send": True, "single_auto_send": False, "parallel_auto_send": False}, False, False),
    # backend deployed before the migration: the old team switch decides both
    ({"auto_drop_enabled": True}, True, True),
    ({"auto_drop_enabled": False}, False, False),
    (None, False, False),
])
def test_effective_auto_send_follows_the_rep_then_the_team(policy, single, parallel):
    assert effective_auto_send(policy, parallel=False) is single
    assert effective_auto_send(policy, parallel=True) is parallel


@pytest.mark.asyncio
async def test_auto_settings_use_the_parallel_policy_only_for_parallel_lines():
    from api.services.telephony.providers.twilio import dialer_call_log
    policy = {"auto_drop_enabled": True, "single_auto_send": False, "parallel_auto_send": True}
    drop = {"id": _DROP, "storage_path": f"{_REP}/m.wav"}
    with patch.object(dialer_call_log, "SUPABASE_URL", "https://db.example.com"), \
         patch.object(dialer_call_log, "SUPABASE_SERVICE_ROLE_KEY", "service"), \
         patch.object(dialer_call_log, "get_team_voicemail_policy", new=AsyncMock(return_value=policy)) as read, \
         patch.object(dialer_call_log, "resolve_effective_voicemail_drop", new=AsyncMock(return_value=drop)), \
         patch.object(dialer_call_log, "sign_voicemail_storage_path", new=AsyncMock(return_value=_URL)), \
         patch.object(dialer_call_log, "verify_voicemail_audio", new=AsyncMock(return_value=True)):
        single = await dialer_call_log.get_auto_voicemail_settings(user_id=_REP, parent_call_sid=_PARENT)
        line = await dialer_call_log.get_auto_voicemail_settings(user_id=_REP, parent_call_sid=_PARENT, parallel=True)
    assert single == (False, None, None)          # the rep chose to be asked
    assert line == (True, _URL, _DROP)            # parallel machines are always dropped
    # Resolved for the call's own rep on the server, never from the browser.
    assert read.await_args.kwargs["user_id"] == _REP


# ── Twilio single call ───────────────────────────────────────────────────────

def _join(answered_by: str = "machine_end_beep", **params):
    return _app().post(
        "/dialer-conference-join",
        params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT, **params},
        data={"AnsweredBy": answered_by, "CallSid": _CHILD},
        headers={"X-Twilio-Signature": "sig"},
    )


def _single_call_mocks(*, enabled: bool, on_machine: str = "drop_vm", claim: bool = True, row: dict | None = None):
    row = row if row is not None else {"rep_user_id": _REP, "child_call_sid": _CHILD}
    return (
        patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()),
        patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)),
        patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()),
        patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)),
        patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": on_machine})),
        patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(
            return_value=(enabled, _URL if enabled else None, _DROP if enabled else None))),
        patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=claim)),
        patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)),
        patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})),
        patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=True)),
    )


def _enter(patches):
    import contextlib
    stack = contextlib.ExitStack()
    mocks = [stack.enter_context(p) for p in patches]
    return stack, mocks


def test_twilio_auto_send_plays_on_the_prospect_leg_and_releases_the_rep_immediately():
    stack, mocks = _enter(_single_call_mocks(enabled=True))
    with stack:
        response = _join()
        settings, claim, cancel = mocks[5], mocks[6], mocks[9]
    assert "<Play>" in response.text and "<Conference" not in response.text
    claim.assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD, manual=False)
    assert settings.await_args.kwargs["parallel"] is False
    # The rep's own leg (never the prospect's) is ended once the TwiML is out.
    cancel.assert_awaited_once_with(call_sid=_PARENT)


def test_twilio_auto_send_off_passes_the_mailbox_to_the_rep_with_no_playback():
    stack, mocks = _enter(_single_call_mocks(enabled=False))
    with stack:
        response = _join()
        claim, cancel = mocks[6], mocks[9]
    assert "<Conference" in response.text and "<Play>" not in response.text
    claim.assert_not_awaited()
    cancel.assert_not_awaited()


def test_twilio_hang_up_policy_still_hangs_up_a_machine():
    stack, mocks = _enter(_single_call_mocks(enabled=False, on_machine="hangup"))
    with stack:
        response = _join()
    assert "<Hangup/>" in response.text and "<Conference" not in response.text


def test_twilio_retried_join_replays_the_claimed_message_instead_of_bridging_it():
    playing = {
        "rep_user_id": _REP, "child_call_sid": _CHILD,
        "voicemail_drop_started_at": "2026-10-19T00:00:00Z", "auto_voicemail_sent": False,
    }
    stack, mocks = _enter(_single_call_mocks(enabled=True, claim=False, row=playing))
    with stack:
        response = _join()
        cancel = mocks[9]
    assert "<Play>" in response.text and "<Conference" not in response.text
    cancel.assert_not_awaited()  # released by the first attempt, not twice


@pytest.mark.parametrize("lead_status", ["initiated", "ringing", "queued", ""])
@pytest.mark.asyncio
async def test_conference_end_after_release_never_cuts_the_message_off(lead_status):
    leg = {
        "child_call_sid": _CHILD, "status": lead_status,
        "voicemail_drop_started_at": "2026-10-19T00:00:00Z", "auto_voicemail_sent": False,
        "manual_voicemail_requested_at": None,
    }
    with patch(f"{_PATH}.dialer_parallel.parallel_session", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=leg)), \
         patch(f"{_PATH}.mark_rep_detached_for_voicemail", new=AsyncMock(return_value=False)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as cancel:
        await twilio_routes._cancel_orphaned_lead_leg(_PARENT)
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_conference_end_still_cancels_an_unanswered_leg_without_playback():
    leg = {"child_call_sid": _CHILD, "status": "ringing", "voicemail_drop_started_at": None}
    with patch(f"{_PATH}.dialer_parallel.parallel_session", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=leg)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as cancel:
        await twilio_routes._cancel_orphaned_lead_leg(_PARENT)
    cancel.assert_awaited_once_with(call_sid=_CHILD)


def test_twilio_one_tap_send_releases_the_rep_after_the_redirect():
    row = {
        "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD,
        "amd_result": "machine_end_beep", "manual_voicemail_requested_at": "2026-10-19T00:00:00Z",
        "voicemail_drop_started_at": None, "auto_voicemail_sent": False,
        "ended_at": None, "lead_joined_at": "2026-10-19T00:00:00Z",
    }
    twilio = MagicMock()
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, _URL, _DROP))), \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.request_manual_voicemail_drop", new=AsyncMock(return_value={"accepted": True, "state": "ready"})), \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}._twilio_client", new=AsyncMock(return_value=twilio)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=True)) as cancel:
        response = _app(authenticated=True).post(
            "/dialer/manual-voicemail-drop", json={"parent_call_sid": _PARENT, "action": "send"},
        )
    assert response.json() == {"state": "playing", "released": True}
    twilio.calls.assert_called_with(_CHILD)            # the message goes on the prospect leg
    cancel.assert_awaited_once_with(call_sid=_PARENT)  # and only the rep's leg is ended


def test_twilio_one_tap_send_keeps_the_rep_when_twilio_refuses_the_redirect():
    row = {
        "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD,
        "amd_result": "machine_end_beep", "manual_voicemail_requested_at": "x",
        "voicemail_drop_started_at": None, "auto_voicemail_sent": False, "lead_joined_at": "x",
    }
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, _URL, _DROP))), \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.request_manual_voicemail_drop", new=AsyncMock(return_value={"accepted": True})), \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}._redirect_twilio_child_to_voicemail", new=AsyncMock(return_value=False)), \
         patch(f"{_PATH}.release_manual_voicemail_playback", new=AsyncMock(return_value=True)) as release, \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as cancel:
        response = _app(authenticated=True).post(
            "/dialer/manual-voicemail-drop", json={"parent_call_sid": _PARENT, "action": "send"},
        )
    assert response.status_code == 502
    release.assert_awaited_once()
    cancel.assert_not_awaited()


def test_twilio_completion_still_records_the_result_on_the_same_row():
    from datetime import UTC, datetime
    started = int(datetime.now(UTC).timestamp() * 1000) - 25_000
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={"child_call_sid": _CHILD})), \
         patch(f"{_PATH}.mark_auto_voicemail_sent", new=AsyncMock(return_value=True)) as sent, \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock()) as failed, \
         patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=False)):
        for _ in range(2):  # Twilio retrying the completion is harmless
            response = _app().post(
                "/dialer-auto-voicemail-complete",
                params={"parent_call_sid": _PARENT, "vm_started": str(started), "vm_expect": "24000"},
                data={"CallSid": _CHILD}, headers={"X-Twilio-Signature": "sig"},
            )
            assert "<Hangup/>" in response.text
    assert all(call.kwargs == {"parent_call_sid": _PARENT, "child_call_sid": None} for call in sent.await_args_list)
    failed.assert_not_awaited()


# ── Twilio parallel ──────────────────────────────────────────────────────────

def test_twilio_parallel_machine_line_is_always_dropped_and_never_releases_the_rep():
    with patch(f"{_PATH}.dialer_parallel.update_parallel_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={"rep_user_id": _REP})), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={
             "on_machine": "drop_vm", "single_auto_send": False, "parallel_auto_send": True})), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, _URL, _DROP))) as settings, \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as cancel:
        response = _join(parallel_leg_id=_LEG, parallel_entry_id=_ENTRY)
    assert "<Play>" in response.text and "<Conference" not in response.text
    assert settings.await_args.kwargs["parallel"] is True
    cancel.assert_not_awaited()  # the batch keeps ringing the other lines


def test_twilio_parallel_machine_without_a_recording_is_never_bridged():
    with patch(f"{_PATH}.dialer_parallel.update_parallel_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={"rep_user_id": _REP})), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": "drop_vm"})), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, None, None))):
        response = _join(parallel_leg_id=_LEG, parallel_entry_id=_ENTRY)
    assert "<Hangup/>" in response.text and "<Conference" not in response.text


def test_twilio_batch_releases_the_rep_once_only_playing_voicemails_remain():
    playing = {"status": "voicemail", "voicemail_drop_started_at": "x", "auto_voicemail_sent": False}
    assert twilio_routes._parallel_batch_has_settled([playing, {"status": "no-answer"}])
    assert not twilio_routes._parallel_batch_has_settled([playing, {"status": "ringing"}])
    # A detected machine that is NOT playing anything is not settled.
    assert not twilio_routes._parallel_batch_has_settled([{"status": "voicemail"}])


# ── SignalWire ───────────────────────────────────────────────────────────────

@pytest.fixture
def _sw_env(monkeypatch):
    from api.tests.test_signalwire_dialer_routes import _SECRET
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)
    yield


def _without_stage_writes(spawned: list) -> list:
    """Background stage-evidence writes are not call control; drop them from counts."""
    kept = []
    for coro in spawned:
        if getattr(coro, "__name__", "") == "_record_stages":
            coro.close()
        else:
            kept.append(coro)
    return kept


_SESSION = "00000000-0000-0000-0000-000000000021"
_SW_LEG = "00000000-0000-0000-0000-000000000022"


def _sw_context(line_count: int):
    return {
        "session": {"id": _SESSION, "status": "dialing", "rep_user_id": _REP, "rep_call_id": "browser-call",
                    "line_count": line_count, "conference_name": f"sysevo-par-{_SESSION}", "winner_leg_id": None},
        "leg": {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "screening"},
    }


def _sw_decide(policy: dict, line_count: int):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    from api.tests.test_signalwire_dialer_routes import _authed
    request = _authed(
        {"call": {"call_id": "prospect-call", "amd_result": "machine", "amd_ms": "2650", "beep": "true"}},
        query={"session_id": _SESSION, "leg_id": _SW_LEG},
    )
    spawned = []
    mocks = {
        "context": patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context(line_count))),
        "update_leg": patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)),
        "policy": patch(f"{_SW}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value=policy)),
        "drop": patch(f"{_SW}._load_signalwire_parallel_default_drop", new=AsyncMock(return_value={
            "id": _DROP, "storage_path": f"{_REP}/m.wav"})),
        "sign": patch(f"{_SW}._signed_signalwire_voicemail_url", new=AsyncMock(return_value="https://db.example.com/m.wav")),
        "claim": patch(f"{_SW}.signalwire_parallel_store.claim_voicemail_drop", new=AsyncMock(return_value=True)),
        "winner": patch(f"{_SW}.signalwire_parallel_store.claim_winner", new=AsyncMock(return_value=True)),
        "outcome": patch(f"{_SW}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)),
        "amd": patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)),
        "endpoints": patch(f"{_SW}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", None))),
        "spawn": patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)),
    }
    return handle_sw_parallel_decision(request), mocks, spawned


@pytest.mark.asyncio
async def test_signalwire_auto_send_plays_and_schedules_the_reps_release(_sw_env):
    policy = {"on_machine": "drop_vm", "single_auto_send": True, "parallel_auto_send": True}
    pending, mocks, spawned = _sw_decide(policy, line_count=1)
    import contextlib
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in mocks.items()}
        response = await pending
    body = json.loads(response.body)
    assert body["sections"]["main"][0]["play"]["url"] == "https://db.example.com/m.wav"
    m["claim"].assert_awaited_once()
    m["winner"].assert_not_awaited()          # never bridged to the rep
    calls = _without_stage_writes(spawned)
    assert len(calls) == 1                    # the release runs straight after
    calls[0].close()


@pytest.mark.asyncio
async def test_signalwire_auto_send_off_bridges_the_mailbox_with_a_machine_result(_sw_env):
    policy = {"on_machine": "drop_vm", "auto_drop_enabled": True, "single_auto_send": False, "parallel_auto_send": True}
    pending, mocks, spawned = _sw_decide(policy, line_count=1)
    import contextlib
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in mocks.items()}
        with patch(f"{_SW}._signalwire_parallel_join_swml", new=AsyncMock(return_value={"sections": {"main": [{"join_conference": {}}]}})):
            response = await pending
    assert "join_conference" in response.body.decode()
    m["winner"].assert_awaited_once()
    m["claim"].assert_not_awaited()
    assert m["amd"].await_args.kwargs["amd_result"] == "machine_end_beep"
    assert _without_stage_writes(spawned) == []


@pytest.mark.asyncio
async def test_signalwire_parallel_machine_line_drops_even_when_the_rep_chose_to_be_asked(_sw_env):
    policy = {"on_machine": "drop_vm", "single_auto_send": False, "parallel_auto_send": True}
    pending, mocks, spawned = _sw_decide(policy, line_count=3)
    import contextlib
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in mocks.items()}
        response = await pending
    assert "play" in json.loads(response.body)["sections"]["main"][0]
    m["winner"].assert_not_awaited()
    for coro in spawned:
        coro.close()


@pytest.mark.asyncio
async def test_signalwire_releases_the_rep_when_only_playing_voicemails_remain():
    from api.services.telephony.dialer import signalwire_routes
    session = {"rep_call_id": "browser-call", "line_count": 1, "winner_leg_id": None}
    with patch(f"{_SW}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=[
            {"id": _SW_LEG, "status": "voicemail_playing"}])), \
         patch(f"{_SW}.signalwire_parallel_store.finish_session", new=AsyncMock(return_value=True)) as finish, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)) as marker, \
         patch("api.services.telephony.dialer.signalwire_calling.end_call", new=AsyncMock()) as end_call:
        assert await signalwire_routes._finish_signalwire_batch_if_settled(session_id=_SESSION, session=session)
    finish.assert_awaited_once_with(session_id=_SESSION, status="failed")
    marker.assert_awaited_once_with(parent_call_sid="browser-call", voicemail_started=True)
    end_call.assert_awaited_once_with(call_id="browser-call", reason="hangup")


@pytest.mark.asyncio
async def test_signalwire_keeps_the_rep_while_another_line_still_rings():
    from api.services.telephony.dialer import signalwire_routes
    with patch(f"{_SW}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=[
            {"id": "a", "status": "voicemail_playing"}, {"id": "b", "status": "ringing"}])), \
         patch(f"{_SW}.signalwire_parallel_store.finish_session", new=AsyncMock()) as finish:
        assert not await signalwire_routes._finish_signalwire_batch_if_settled(
            session_id=_SESSION, session={"rep_call_id": "browser-call", "line_count": 3})
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalwire_late_release_after_the_session_closed_does_nothing_twice():
    from api.services.telephony.dialer import signalwire_routes
    with patch(f"{_SW}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=[
            {"id": _SW_LEG, "status": "voicemail"}])), \
         patch(f"{_SW}.signalwire_parallel_store.finish_session", new=AsyncMock(return_value=False)), \
         patch("api.services.telephony.dialer.signalwire_calling.end_call", new=AsyncMock()) as end_call:
        assert not await signalwire_routes._finish_signalwire_batch_if_settled(
            session_id=_SESSION, session={"rep_call_id": "browser-call", "line_count": 1})
    end_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalwire_playback_result_is_recorded_once_after_the_rep_left(_sw_env):
    """The play callback lands after the session was closed by the release."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_playback
    from api.tests.test_signalwire_dialer_routes import _authed
    context = {
        "session": {"id": _SESSION, "status": "failed", "rep_user_id": _REP, "rep_call_id": "browser-call", "line_count": 1},
        "leg": {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "voicemail_playing"},
    }
    request = _authed({"params": {"call_id": "prospect-call", "state": "finished"}},
                      query={"session_id": _SESSION, "leg_id": _SW_LEG})
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)), \
         patch(f"{_SW}.signalwire_parallel_store.complete_voicemail_drop", new=AsyncMock(return_value=True)) as complete, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)) as amd, \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_SW}._finish_signalwire_batch_if_settled", new=AsyncMock(return_value=False)):
        response = await handle_sw_parallel_playback(request)
    assert json.loads(response.body) == {"ok": True}
    complete.assert_awaited_once()          # one RPC writes outcome + attempt, idempotently
    amd.assert_awaited_once_with(parent_call_sid="prospect-call", voicemail_sent=True)


# ── SignalWire one-tap "Send my voicemail" ──────────────────────────────────

def _sw_app():
    from types import SimpleNamespace
    from api.services.telephony.dialer.signalwire_routes import router as sw_router
    app = FastAPI()
    app.include_router(sw_router)
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(provider_id=_REP)
    return TestClient(app)


def _bridged_mailbox(status: str = "connected"):
    return {
        "session": {"id": _SESSION, "rep_user_id": _REP, "rep_call_id": "browser-call", "status": "connected",
                    "winner_leg_id": _SW_LEG, "line_count": 1, "conference_name": f"sysevo-par-{_SESSION}"},
        "leg": {"id": _SW_LEG, "provider_call_id": "prospect-call", "entry_id": _ENTRY,
                "status": status, "amd_result": "machine", "voicemail_started_at": None, "voicemail_sent_at": None},
    }


def test_signalwire_status_reports_a_bridged_mailbox_as_ready():
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep",
               new=AsyncMock(return_value=_bridged_mailbox())) as lookup:
        body = _sw_app().get("/dialer/signalwire/voicemail-drop/browser-call").json()
    assert body == {"state": "idle", "amd_result": "machine_end_other", "ready": True, "stage": None, "stage_at": None, "stage_detail": {}}
    lookup.assert_awaited_once_with(rep_call_id="browser-call", rep_user_id=_REP)


def test_signalwire_status_is_404_for_a_keypad_call():
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep", new=AsyncMock(return_value=None)):
        assert _sw_app().get("/dialer/signalwire/voicemail-drop/browser-call").status_code == 404


def test_signalwire_one_tap_send_plays_on_the_prospect_leg_and_releases_the_rep():
    spawned = []
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep", new=AsyncMock(return_value=_bridged_mailbox())), \
         patch(f"{_SW}._load_signalwire_parallel_default_drop", new=AsyncMock(return_value={"id": _DROP, "storage_path": f"{_REP}/m.wav"})), \
         patch(f"{_SW}._signed_signalwire_voicemail_url", new=AsyncMock(return_value="https://db.example.com/m.wav")), \
         patch(f"{_SW}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", None))), \
         patch(f"{_SW}.signalwire_parallel_store.claim_manual_voicemail", new=AsyncMock(return_value=True)) as claim, \
         patch("api.services.telephony.dialer.signalwire_calling.update_call_swml", new=AsyncMock()) as update, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)):
        response = _sw_app().post("/dialer/signalwire/voicemail-drop", json={"rep_call_id": "browser-call"})
    assert response.json() == {"state": "playing", "released": True}
    claim.assert_awaited_once_with(session_id=_SESSION, leg_id=_SW_LEG, provider_call_id="prospect-call",
                                   drop_id=_DROP, rep_user_id=_REP)
    assert update.await_args.kwargs["call_id"] == "prospect-call"
    assert update.await_args.kwargs["swml"]["sections"]["main"][0]["play"]["url"] == "https://db.example.com/m.wav"
    calls = _without_stage_writes(spawned)
    assert len(calls) == 1  # releases the rep and closes the session
    calls[0].close()


def test_signalwire_one_tap_send_gives_the_line_back_when_signalwire_refuses():
    from api.services.telephony.dialer.signalwire_calling import SignalWireCallError
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep", new=AsyncMock(return_value=_bridged_mailbox())), \
         patch(f"{_SW}._load_signalwire_parallel_default_drop", new=AsyncMock(return_value={"id": _DROP, "storage_path": f"{_REP}/m.wav"})), \
         patch(f"{_SW}._signed_signalwire_voicemail_url", new=AsyncMock(return_value="https://db.example.com/m.wav")), \
         patch(f"{_SW}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", None))), \
         patch(f"{_SW}.signalwire_parallel_store.claim_manual_voicemail", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.release_manual_voicemail", new=AsyncMock(return_value=True)) as release, \
         patch("api.services.telephony.dialer.signalwire_calling.update_call_swml",
               new=AsyncMock(side_effect=SignalWireCallError("HTTP 422"))), \
         patch(f"{_SW}._spawn_background") as spawn:
        response = _sw_app().post("/dialer/signalwire/voicemail-drop", json={"rep_call_id": "browser-call"})
    assert response.status_code == 502
    release.assert_awaited_once()
    spawn.assert_not_called()  # the rep stays on the call to speak instead


def test_signalwire_double_tap_does_not_play_twice():
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep",
               new=AsyncMock(return_value=_bridged_mailbox(status="voicemail_playing"))), \
         patch(f"{_SW}.signalwire_parallel_store.claim_manual_voicemail", new=AsyncMock()) as claim:
        response = _sw_app().post("/dialer/signalwire/voicemail-drop", json={"rep_call_id": "browser-call"})
    assert response.json() == {"state": "playing"}
    claim.assert_not_awaited()


# ── "Send my voicemail" pressed at every moment of a call (top priority) ────
#
# The rep is released IMMEDIATELY whenever the line can still take a message;
# a person on the line is refused and the rep keeps the call.

def _twilio_press(row: dict, *, request_result: dict, detection: bool = True, detached: bool = True):
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, _URL, _DROP))), \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.request_manual_voicemail_drop", new=AsyncMock(return_value=request_result)), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"machine_detection_enabled": detection})), \
         patch(f"{_PATH}.mark_rep_detached_for_voicemail", new=AsyncMock(return_value=detached)) as detach, \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)) as claim, \
         patch(f"{_PATH}._redirect_twilio_child_to_voicemail", new=AsyncMock(return_value=True)) as redirect, \
         patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=True)) as cancel:
        response = _app(authenticated=True).post(
            "/dialer/manual-voicemail-drop", json={"parent_call_sid": _PARENT, "action": "send"},
        )
    return response, {"detach": detach, "claim": claim, "redirect": redirect, "cancel": cancel}


def _twilio_row(**overrides):
    row = {
        "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD, "status": "ringing",
        "amd_result": None, "manual_voicemail_requested_at": "2026-10-19T00:00:00Z",
        "voicemail_drop_started_at": None, "auto_voicemail_sent": False,
        "ended_at": None, "lead_joined_at": None,
    }
    return {**row, **overrides}


@pytest.mark.parametrize("lead_status", ["ringing", "in-progress"])  # ringing / AMD holding the greeting
def test_twilio_press_before_amd_hands_the_line_over_and_releases_the_rep(lead_status):
    response, m = _twilio_press(_twilio_row(status=lead_status), request_result={"accepted": True, "state": "waiting"})
    assert response.json() == {"state": "waiting", "released": True}
    m["detach"].assert_awaited_once_with(parent_call_sid=_PARENT)  # a person later is hung up, never bridged
    m["cancel"].assert_awaited_once_with(call_sid=_PARENT)         # the rep's leg, immediately
    m["redirect"].assert_not_awaited()                             # nothing plays until AMD confirms a machine


def test_twilio_queued_press_then_machine_plays_in_the_background():
    """After the release, Twilio's signed AMD answer plays the recording."""
    row = _twilio_row(manual_voicemail_requested_at="2026-10-19T00:00:00Z")
    stack, mocks = _enter(_single_call_mocks(enabled=False, row=row))
    with stack:
        mocks[8].return_value = {"rep_detached_at": "2026-10-19T00:00:01Z"}
        mocks[5].return_value = (False, _URL, _DROP)  # a manual press still resolves the recording
        response = _join("machine_end_silence")
        assert mocks[5].await_args.kwargs["allow_manual_when_disabled"] is True
        claim = mocks[6]
    assert "<Play>" in response.text and "<Conference" not in response.text
    claim.assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD, manual=True)


def test_twilio_queued_press_then_a_person_is_never_bridged_into_an_empty_room():
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={"rep_detached_at": "x"})), \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)) as failed:
        response = _join("human")
    assert "<Hangup/>" in response.text and "<Play>" not in response.text and "<Conference" not in response.text
    # The rep's background status must say "call them back", not "no mailbox".
    failed.assert_awaited_once_with(parent_call_sid=_PARENT, reason="person_answered")


def test_twilio_queued_press_then_unplayable_machine_is_not_reported_as_a_person():
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={"rep_detached_at": "x"})), \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)) as failed:
        _join("fax")
    failed.assert_not_awaited()


def test_twilio_press_on_a_detected_mailbox_plays_now_and_releases_the_rep():
    row = _twilio_row(status="in-progress", amd_result="machine_end_beep", lead_joined_at="2026-10-19T00:00:02Z")
    response, m = _twilio_press(row, request_result={"accepted": True, "state": "ready"})
    assert response.json() == {"state": "playing", "released": True}
    m["redirect"].assert_awaited_once()
    m["cancel"].assert_awaited_once_with(call_sid=_PARENT)


def test_twilio_press_with_a_person_on_the_line_is_refused_and_keeps_the_rep():
    row = _twilio_row(status="in-progress", amd_result="human", lead_joined_at="2026-10-19T00:00:02Z",
                      manual_voicemail_requested_at=None)
    response, m = _twilio_press(row, request_result={"accepted": False, "reason": "not_voicemail"})
    assert response.status_code == 409
    m["cancel"].assert_not_awaited()
    m["detach"].assert_not_awaited()
    m["redirect"].assert_not_awaited()


def test_twilio_press_before_amd_keeps_the_rep_when_detection_is_off():
    """With no machine detection nothing could ever confirm the mailbox."""
    response, m = _twilio_press(_twilio_row(), request_result={"accepted": True, "state": "waiting"}, detection=False)
    assert response.json() == {"state": "waiting"}
    m["cancel"].assert_not_awaited()


def _sw_leg_found(leg_status: str, amd: str | None = None, session_status: str = "dialing", winner: str | None = None):
    return {
        "session": {"id": _SESSION, "rep_user_id": _REP, "rep_call_id": "browser-call", "status": session_status,
                    "winner_leg_id": winner, "line_count": 1, "conference_name": f"sysevo-par-{_SESSION}"},
        "leg": {"id": _SW_LEG, "provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": leg_status,
                "amd_result": amd, "voicemail_started_at": None, "voicemail_sent_at": None,
                "manual_voicemail_requested_at": None},
    }


@pytest.mark.parametrize("leg_status", ["ringing", "screening"])  # ringing / AMD in the greeting
def test_signalwire_press_before_amd_queues_it_and_releases_the_rep(leg_status):
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep",
               new=AsyncMock(return_value=_sw_leg_found(leg_status))), \
         patch(f"{_SW}.signalwire_parallel_store.request_manual_voicemail", new=AsyncMock(return_value="waiting")) as queue, \
         patch("api.services.telephony.dialer.signalwire_calling.end_call", new=AsyncMock()) as end_call, \
         patch("api.services.telephony.dialer.signalwire_calling.update_call_swml", new=AsyncMock()) as update:
        response = _sw_app().post("/dialer/signalwire/voicemail-drop", json={"rep_call_id": "browser-call"})
    assert response.json() == {"state": "waiting", "released": True}
    queue.assert_awaited_once_with(session_id=_SESSION, leg_id=_SW_LEG, rep_user_id=_REP)
    end_call.assert_awaited_once_with(call_id="browser-call", reason="hangup")
    update.assert_not_awaited()


def test_signalwire_press_with_a_person_on_the_line_is_refused_and_keeps_the_rep():
    found = _sw_leg_found("connected", amd="human", session_status="connected", winner=_SW_LEG)
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep", new=AsyncMock(return_value=found)), \
         patch(f"{_SW}.signalwire_parallel_store.request_manual_voicemail", new=AsyncMock()) as queue, \
         patch("api.services.telephony.dialer.signalwire_calling.end_call", new=AsyncMock()) as end_call, \
         patch(f"{_SW}.signalwire_parallel_store.claim_manual_voicemail", new=AsyncMock()) as claim:
        response = _sw_app().post("/dialer/signalwire/voicemail-drop", json={"rep_call_id": "browser-call"})
    assert response.status_code == 409
    queue.assert_not_awaited()
    claim.assert_not_awaited()
    end_call.assert_not_awaited()


def test_signalwire_status_shows_a_queued_request_as_waiting():
    found = _sw_leg_found("screening")
    found["leg"]["manual_voicemail_requested_at"] = "2026-10-19T00:00:00Z"
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep", new=AsyncMock(return_value=found)):
        body = _sw_app().get("/dialer/signalwire/voicemail-drop/browser-call").json()
    assert body["state"] == "waiting"


@pytest.mark.asyncio
async def test_signalwire_queued_press_then_machine_plays_even_if_auto_send_is_off(_sw_env):
    policy = {"on_machine": "drop_vm", "single_auto_send": False, "parallel_auto_send": True}
    pending, mocks, spawned = _sw_decide(policy, line_count=1)
    import contextlib
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in mocks.items()}
        stack.enter_context(patch(f"{_SW}.signalwire_parallel_store.leg_voicemail_requested", new=AsyncMock(return_value=True)))
        response = await pending
    assert "play" in json.loads(response.body)["sections"]["main"][0]
    m["winner"].assert_not_awaited()  # the rep has left: never bridged
    for coro in spawned:
        coro.close()


@pytest.mark.asyncio
async def test_signalwire_queued_press_then_a_person_is_hung_up_not_bridged(_sw_env):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    from api.tests.test_signalwire_dialer_routes import _authed
    request = _authed({"call": {"call_id": "prospect-call", "amd_result": "human", "amd_ms": "900"}},
                      query={"session_id": _SESSION, "leg_id": _SW_LEG})
    spawned = []
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context(1))), \
         patch(f"{_SW}.signalwire_parallel_store.leg_voicemail_requested", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)) as update_leg, \
         patch(f"{_SW}.signalwire_parallel_store.claim_winner", new=AsyncMock()) as winner, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)):
        response = await handle_sw_parallel_decision(request)
    assert json.loads(response.body) == {"sections": {"main": [{"hangup": {}}]}}
    winner.assert_not_awaited()
    assert update_leg.await_args.kwargs["status"] == "completed"
    for coro in spawned:
        coro.close()


@pytest.mark.asyncio
async def test_signalwire_rep_leaving_after_queuing_does_not_cancel_the_line():
    from api.services.telephony.dialer import signalwire_routes
    with patch(f"{_SW}.signalwire_parallel_store.get_session", new=AsyncMock(return_value={"status": "dialing"})), \
         patch(f"{_SW}.signalwire_parallel_store.session_rep_released", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.finish_session", new=AsyncMock()) as finish, \
         patch(f"{_SW}._cancel_signalwire_ringing_siblings", new=AsyncMock()) as cancel:
        await signalwire_routes._abandon_signalwire_batch_after_rep_left(session_id=_SESSION)
    finish.assert_not_awaited()
    cancel.assert_not_awaited()


def test_signalwire_status_says_a_person_answered_after_the_rep_left():
    from api.services.telephony.dialer.signalwire_routes import _signalwire_drop_state
    found = {
        "session": {"winner_leg_id": None},
        "leg": {"id": "leg-1", "status": "completed", "amd_result": "human",
                "manual_voicemail_requested_at": "2026-10-20T00:00:00Z"},
    }
    state = _signalwire_drop_state(found)
    assert state["state"] == "rejected" and state["failure"] == "person_answered"
    no_answer = {**found, "leg": {**found["leg"], "amd_result": None}}
    assert "failure" not in _signalwire_drop_state(no_answer)


def test_twilio_person_is_never_bridged_when_the_rep_leg_already_ended():
    """21 Oct: the rep's browser leg failed at 0s, the prospect still answered and sat
    alone in a silent, unrecorded conference for 1m55s. Now: never joined."""
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})), \
         patch(f"{_PATH}._rep_leg_ended", new=AsyncMock(return_value=True)) as ended, \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)) as failed:
        response = _join("human")
    ended.assert_awaited_once_with(_PARENT)
    assert "<Conference" not in response.text
    # Not a voicemail hand-off, so nothing is reported as a failed voicemail.
    failed.assert_not_awaited()


def test_twilio_person_joins_as_before_while_the_rep_leg_is_live():
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})), \
         patch(f"{_PATH}._rep_leg_ended", new=AsyncMock(return_value=False)):
        response = _join("human")
    assert "<Conference" in response.text
