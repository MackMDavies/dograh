"""Dialer Phase 1 (voicemail): stages, recording lock, ring-back and mailbox full.

* every voicemail/screening step is recorded as a stage, and the status polls carry
  the latest `stage` / `stage_at` (a ring-back reads as state `ringback`);
* a plain rep without a default WAV is refused on every outbound start path with
  `voicemail_recording_required` (manager/closer/admin never; inbound never);
* a person who answers after the rep pressed "Send voicemail" rings the ORIGINAL rep
  back when they are free, otherwise today's "person_answered" path; the recording is
  never played to a person;
* a playback the mailbox ends within seconds is `mailbox_full`, never "left";
* the call sorter hook (`report_listening_stage`) only accepts its five stages.

Every provider and database boundary is mocked; no call is placed.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.dialer import call_stages, extra_answer
from api.services.telephony.dialer.swml import (
    build_say_and_hangup_swml,
    build_signalwire_ringback_wait_swml,
)
from api.services.telephony.providers.twilio import dialer_voicemail_ringback
from api.services.telephony.providers.twilio.routes import router

_PATH = "api.services.telephony.providers.twilio.routes"
_SW = "api.services.telephony.dialer.signalwire_routes"
_STAGES = "api.services.telephony.dialer.call_stages"
_RINGBACK = "api.services.telephony.providers.twilio.dialer_voicemail_ringback"
_PARENT = "CA" + "abcdef0123456789" * 2
_CHILD = "CA" + "0123456789abcdef" * 2
_REP = "11111111-1111-1111-1111-111111111111"
_SESSION = "00000000-0000-0000-0000-000000000021"
_SW_LEG = "00000000-0000-0000-0000-000000000022"
_ENTRY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _signed(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    monkeypatch.setattr(f"{_PATH}.close_floor_monitor_for_provider", AsyncMock(return_value=False))
    with patch(f"{_PATH}.RequestValidator.validate", return_value=True), \
         patch(f"{_PATH}._resolve_dialer_auth_token", new=AsyncMock(return_value="test-auth-token")):
        yield


def _app(authenticated: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if authenticated:
        app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(id=7, provider_id=_REP)
    return TestClient(app)


# ── stages: recorder, status payload, sorter hook ─────────────────────────────

@pytest.mark.asyncio
async def test_record_call_stage_rejects_unknown_stages_and_bad_keys():
    with patch(f"{_STAGES}._ready", return_value=True), \
         patch(f"{_STAGES}._rpc", new=AsyncMock(return_value="00000000-0000-0000-0000-0000000000e1")) as rpc:
        assert await call_stages.record_call_stage(_PARENT, "vm_left", {"x": 1}) is True
        assert await call_stages.record_call_stage(_PARENT, "nonsense") is False
        assert await call_stages.record_call_stage("short", "vm_left") is False
    rpc.assert_awaited_once_with("dialer_record_call_stage", {
        "p_parent_call_sid": _PARENT, "p_stage": "vm_left", "p_detail": {"x": 1},
    })


@pytest.mark.asyncio
async def test_record_call_stage_never_raises():
    with patch(f"{_STAGES}._ready", return_value=True), \
         patch(f"{_STAGES}._rpc", new=AsyncMock(side_effect=RuntimeError("db down"))):
        assert await call_stages.record_call_stage(_PARENT, "machine") is False


def test_with_stage_adds_stage_fields_and_reports_a_ringback():
    plain = call_stages.with_stage({"state": "waiting"}, {"stage": "machine", "stage_at": "t1"})
    assert plain == {"state": "waiting", "stage": "machine", "stage_at": "t1", "stage_detail": {}}
    ringing_back = call_stages.with_stage(
        {"state": "rejected", "failure": "person_answered"}, {"stage": "rep_ringback", "stage_at": "t2"},
    )
    assert ringing_back == {"state": "ringback", "stage": "rep_ringback", "stage_at": "t2", "stage_detail": {}}
    # A missed ring-back records 'person' after it: today's toast applies again.
    missed = call_stages.with_stage(
        {"state": "failed", "failure": "person_answered"}, {"stage": "person", "stage_at": "t3"},
    )
    assert missed["state"] == "failed" and missed["failure"] == "person_answered"
    assert call_stages.with_stage({"state": "idle"}, None) == {"state": "idle", "stage": None, "stage_at": None, "stage_detail": {}}


@pytest.mark.asyncio
async def test_listening_hook_accepts_only_sorter_stages_and_fails_a_full_mailbox():
    with patch(f"{_STAGES}.record_call_stage", new=AsyncMock(return_value=True)) as record, \
         patch("api.services.telephony.providers.twilio.dialer_call_log.record_voicemail_playback_failure",
               new=AsyncMock(return_value=True)) as failed:
        assert await call_stages.report_listening_stage(call_key=_PARENT, stage="screener") is True
        await call_stages.report_listening_stage(call_key=_PARENT, stage="mailbox_full", detail={"phrase": "full"})
        with pytest.raises(ValueError):
            await call_stages.report_listening_stage(call_key=_PARENT, stage="vm_left")
    assert record.await_args_list[0].args == (_PARENT, "screener", {"source": "listening"})
    failed.assert_awaited_once_with(parent_call_sid=_PARENT, reason="mailbox_full")


def test_twilio_status_poll_carries_the_latest_stage():
    row = {"child_call_sid": _CHILD, "provider": "twilio", "rep_user_id": _REP, "amd_result": "human",
           "manual_voicemail_requested_at": "2026-10-20T00:00:00Z"}
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})), \
         patch(f"{_PATH}.call_stages.latest_call_stage", new=AsyncMock(
             return_value={"stage": "rep_ringback", "stage_at": "2026-10-20T00:00:05Z"})) as latest:
        body = _app(authenticated=True).get(f"/dialer/manual-voicemail-drop/{_PARENT}").json()
    latest.assert_awaited_once_with(_PARENT)
    assert body == {"state": "ringback", "amd_result": "human", "ready": True,
                    "stage": "rep_ringback", "stage_at": "2026-10-20T00:00:05Z", "stage_detail": {}}


def test_signalwire_status_poll_carries_the_latest_stage():
    from api.services.telephony.dialer.signalwire_routes import router as sw_router
    app = FastAPI()
    app.include_router(sw_router)
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(provider_id=_REP)
    found = {
        "session": {"id": _SESSION, "winner_leg_id": None, "status": "dialing", "line_count": 1},
        "leg": {"id": _SW_LEG, "status": "ringing", "amd_result": None,
                "manual_voicemail_requested_at": "2026-10-20T00:00:00Z"},
    }
    with patch(f"{_SW}.signalwire_parallel_store.get_screened_single_for_rep", new=AsyncMock(return_value=found)), \
         patch(f"{_SW}.call_stages.latest_call_stage", new=AsyncMock(
             return_value={"stage": "vm_requested", "stage_at": "2026-10-20T00:00:01Z"})):
        body = TestClient(app).get("/dialer/signalwire/voicemail-drop/browser-call").json()
    assert body["state"] == "waiting"
    assert body["stage"] == "vm_requested" and body["stage_at"] == "2026-10-20T00:00:01Z"


# ── recording lock ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "locked"), [
    ({"lock": True, "warn": False, "role": "rep", "has_default_recording": False}, True),
    ({"lock": False, "warn": True, "role": "manager", "has_default_recording": False}, False),
    ({"lock": False, "warn": False, "role": "rep", "has_default_recording": True}, False),
    (None, False),
])
async def test_recording_lock_reads_server_readiness(result, locked):
    with patch(f"{_STAGES}._ready", return_value=True), \
         patch(f"{_STAGES}._rpc", new=AsyncMock(return_value=result)) as rpc:
        assert await call_stages.voicemail_recording_required(_REP) is locked
    rpc.assert_awaited_once()
    assert rpc.await_args.args == ("dialer_user_voicemail_readiness", {"p_user_id": _REP})


@pytest.mark.asyncio
async def test_recording_lock_fails_open_when_readiness_is_unreadable():
    # The SQL insert guard still refuses a locked rep; an outage must not stop everyone.
    with patch(f"{_STAGES}._ready", return_value=True), \
         patch(f"{_STAGES}._rpc", new=AsyncMock(side_effect=RuntimeError("timeout"))):
        assert await call_stages.voicemail_recording_required(_REP) is False


def test_twilio_voice_connect_refuses_a_locked_rep_before_any_call_row_or_dial():
    with patch(f"{_PATH}.resolve_assigned_caller_id", new=AsyncMock(return_value="+15551234567")), \
         patch(f"{_PATH}.db_client.get_user_by_id", new=AsyncMock(return_value=SimpleNamespace(provider_id=_REP))), \
         patch(f"{_PATH}.get_dialer_phone_block_reason", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}.check_dialer_calling_hours_with_state", new=AsyncMock(return_value=(True, None, None))), \
         patch(f"{_PATH}.call_stages.voicemail_recording_required", new=AsyncMock(return_value=True)) as lock, \
         patch(f"{_PATH}.create_dialer_call", new=AsyncMock(return_value=True)) as create, \
         patch(f"{_PATH}.dial_lead_into_conference", new=AsyncMock()) as dial:
        response = _app().post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": _PARENT, "From": "client:rep-42"},
            headers={"X-Twilio-Signature": "sig"},
        )
    lock.assert_awaited_once_with(_REP)
    assert "Record your voicemail to start calling." in response.text and "<Hangup/>" in response.text
    create.assert_not_awaited()
    dial.assert_not_awaited()


def test_telnyx_start_refuses_a_locked_rep_with_the_contract_code():
    from uuid import uuid4
    with patch(f"{_PATH}._get_assigned_dialer_number_or_503", new=AsyncMock(return_value={
             "provider": "telnyx", "direction": "both", "phone_number": "+15550001111"})), \
         patch(f"{_PATH}.phone_block_reason", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}.check_dialer_calling_hours_with_state", new=AsyncMock(return_value=(True, None, None))), \
         patch(f"{_PATH}.call_stages.voicemail_recording_required", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.create_dialer_call", new=AsyncMock(return_value=True)) as create:
        response = _app(authenticated=True).post(
            "/dialer/telnyx/calls/start", json={"call_id": str(uuid4()), "to_number": "+15559876543"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "voicemail_recording_required"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalwire_single_call_refuses_a_locked_rep():
    from api.services.telephony.dialer.signalwire_routes import handle_sw_dialer_connect
    from api.tests.test_signalwire_dialer_routes import _authed
    with patch(f"{_SW}._rep_supabase_id", new=AsyncMock(return_value=_REP)), \
         patch(f"{_SW}.resolve_assigned_caller_id", new=AsyncMock(return_value=None)), \
         patch(f"{_SW}._is_signalwire_owned_number", new=AsyncMock(return_value=False)), \
         patch(f"{_SW}.call_stages.voicemail_recording_required", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.create_dialer_call", new=AsyncMock()) as create, \
         patch.dict("os.environ", {"SIGNALWIRE_DEFAULT_CALLER_ID": "+12092669253"}):
        from api.tests.test_signalwire_dialer_routes import _SECRET
        with patch.dict("os.environ", {"SIGNALWIRE_WEBHOOK_KEY": _SECRET}):
            response = await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    document = json.loads(response.body)
    assert document == build_say_and_hangup_swml("Record your voicemail to start calling.")
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalwire_screened_session_refuses_a_locked_rep_before_reserving():
    from api.services.telephony.dialer import signalwire_routes
    from api.tests.test_signalwire_dialer_routes import _authed
    with patch(f"{_SW}._rep_supabase_id", new=AsyncMock(return_value=_REP)), \
         patch("api.services.telephony.dialer.parallel_auth.verify_parallel_auth_token", return_value=_REP), \
         patch(f"{_SW}.call_stages.voicemail_recording_required", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.start_screened_single_session", new=AsyncMock()) as start:
        response = await signalwire_routes._start_signalwire_session(
            request=_authed({}), payload={"EntryId": _ENTRY, "ParallelAuthToken": "t"}, query={},
            identity="rep-42", screened_single=True,
        )
    assert json.loads(response.body)["sections"]["main"][-1] == {"hangup": {}}
    start.assert_not_awaited()


# ── Twilio: ring-time Send voicemail, beep, ring-back ────────────────────────

def _join(answered_by: str, **params):
    return _app().post(
        "/dialer-conference-join",
        params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT, **params},
        data={"AnsweredBy": answered_by, "CallSid": _CHILD},
        headers={"X-Twilio-Signature": "sig"},
    )


def _detached_person_mocks(*, ringback: str | None):
    return [
        patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()),
        patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)),
        patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()),
        patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={"rep_detached_at": "x"})),
        patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)),
        patch(f"{_PATH}.dialer_voicemail_ringback.offer_ringback_twiml", new=AsyncMock(return_value=ringback)),
        patch(f"{_PATH}.dialer_voicemail_ringback.expire_ringback", new=AsyncMock()),
        patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock(return_value=True)),
    ]


def _enter(patches):
    import contextlib
    stack = contextlib.ExitStack()
    return stack, [stack.enter_context(p) for p in patches]


def test_twilio_person_after_the_hand_off_rings_the_original_rep_back():
    wait = '<?xml version="1.0"?><Response><Dial><Conference>call-x</Conference></Dial></Response>'
    stack, m = _enter(_detached_person_mocks(ringback=wait))
    with stack:
        response = _join("human")
    failed, offer, expire = m[4], m[5], m[6]
    assert response.text == wait
    assert offer.await_args.kwargs["parent_call_sid"] == _PARENT
    assert offer.await_args.kwargs["prospect_call_sid"] == _CHILD
    expire.assert_awaited_once_with(parent_call_sid=_PARENT, prospect_call_sid=_CHILD)
    failed.assert_not_awaited()                   # not "picked up after you moved on" (yet)
    assert "<Play>" not in response.text          # never the recording to a person


def test_twilio_person_after_the_hand_off_with_a_busy_rep_keeps_todays_path():
    stack, m = _enter(_detached_person_mocks(ringback=None))
    with stack:
        response = _join("human")
    failed, expire, record = m[4], m[6], m[7]
    assert "<Hangup/>" in response.text and "<Conference" not in response.text
    failed.assert_awaited_once_with(parent_call_sid=_PARENT, reason="person_answered")
    expire.assert_not_awaited()
    assert record.await_args.args[:2] == (_PARENT, "person")
    assert record.await_args.args[2]["ringback"] == "unavailable"


def test_twilio_machine_after_the_hand_off_never_rings_the_rep_back():
    stack, m = _enter(_detached_person_mocks(ringback="<Response/>"))
    with stack:
        _join("fax")
    m[5].assert_not_awaited()


def test_twilio_mailbox_passed_to_the_rep_records_machine_then_beep():
    row = {"rep_user_id": _REP, "child_call_sid": _CHILD}
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(
             return_value={"on_machine": "drop_vm", "voicemail_mode": "self"})), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, None, None))), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})), \
         patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        response = _join("machine_end_beep")
    assert "<Conference" in response.text and "<Play>" not in response.text
    stages = [c.args[1] for c in record.await_args_list]
    assert stages == ["machine", "beep"]
    # 'machine' is the AMD decision; 'beep' is the tone Twilio heard.
    assert record.await_args_list[0].args[2] == {"answered_by": "machine_end_beep", "mode": "self", "source": "amd"}
    assert record.await_args_list[1].args[2] == {"answered_by": "machine_end_beep", "mode": "self", "source": "tone"}


def test_twilio_auto_playback_records_machine_then_vm_playing():
    row = {"rep_user_id": _REP, "child_call_sid": _CHILD}
    url = "https://db.example.com/storage/v1/object/sign/voicemail-drops/u/m.wav?token=x"
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": "drop_vm"})), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, url, "d1"))), \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        response = _join("machine_end_silence")
    assert "<Play>" in response.text
    assert [c.args[1] for c in record.await_args_list] == ["machine", "vm_playing"]


def test_twilio_full_playback_records_vm_left():
    from datetime import UTC, datetime
    started = int(datetime.now(UTC).timestamp() * 1000) - 30_000
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={"child_call_sid": _CHILD})), \
         patch(f"{_PATH}.mark_auto_voicemail_sent", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        _app().post(
            "/dialer-auto-voicemail-complete",
            params={"parent_call_sid": _PARENT, "vm_started": str(started), "vm_expect": "24000"},
            data={"CallSid": _CHILD}, headers={"X-Twilio-Signature": "sig"},
        )
    record.assert_awaited_once_with(_PARENT, "vm_left", {})


def _status(call_status: str, row: dict | None, settle=None):
    settle = settle or AsyncMock(return_value=None)
    with patch(f"{_PATH}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_PATH}.settle_interrupted_playback", new=settle), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)) as failed, \
         patch(f"{_PATH}.close_inbound_call", new=AsyncMock()) as close, \
         patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        _app().post(
            "/dialer-call-status", params={"parent_call_sid": _PARENT},
            data={"CallSid": _CHILD, "CallStatus": call_status}, headers={"X-Twilio-Signature": "sig"},
        )
    return failed, close, record


def test_twilio_playback_the_carrier_ends_is_settled_by_its_length():
    """Mailbox full vs cut short vs left is measured in SQL against the recording's own
    length (dialer_settle_interrupted_playback); the route always asks for a settlement."""
    from datetime import UTC, datetime, timedelta
    row = {"child_call_sid": _CHILD, "voicemail_drop_started_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
           "auto_voicemail_sent": False, "manual_voicemail_requested_at": None}
    settle = AsyncMock(return_value="mailbox_full")
    failed, _close, _record = _status("completed", row, settle)
    settle.assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD)
    failed.assert_not_awaited()  # the RPC writes the failure itself


def test_twilio_sent_playback_is_never_settled_again():
    from datetime import UTC, datetime, timedelta
    row = {"child_call_sid": _CHILD, "voicemail_drop_started_at": (datetime.now(UTC) - timedelta(seconds=20)).isoformat(),
           "auto_voicemail_sent": True}
    settle = AsyncMock(return_value=None)
    failed, _close, record = _status("completed", row, settle)
    settle.assert_not_awaited()
    failed.assert_not_awaited()
    record.assert_not_awaited()


def test_twilio_ring_out_records_no_answer_and_closes_a_ringback_offer():
    row = {"child_call_sid": _CHILD, "manual_voicemail_requested_at": "2026-10-20T00:00:00Z"}
    _failed, close, record = _status("no-answer", row)
    assert record.await_args_list[0].args == (_PARENT, "no_answer", {"status": "no-answer"})
    close.assert_awaited_once_with(provider_call_id=_CHILD)


def test_twilio_ringing_records_the_ringing_stage():
    _failed, _close, record = _status("ringing", None)
    record.assert_awaited_once_with(_PARENT, "ringing", {})


def test_twilio_rep_joining_a_ringback_conference_records_rejoined():
    conference = f"call-{_CHILD}"
    with patch(f"{_PATH}.db_client.get_user_by_id", new=AsyncMock(return_value=SimpleNamespace(provider_id=_REP))), \
         patch(f"{_PATH}.is_claimed_inbound_conference", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.bind_inbound_rep_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.com", ""))), \
         patch(f"{_PATH}._live_transcription_twiml", new=AsyncMock(return_value="")), \
         patch(f"{_PATH}.call_stages.record_ringback_joined", new=AsyncMock(return_value=True)) as joined:
        response = _app().post(
            "/voice-connect",
            data={"conference": conference, "From": "client:rep-42", "CallSid": _PARENT},
            headers={"X-Twilio-Signature": "sig"},
        )
    assert conference in response.text
    joined.assert_awaited_once_with(conference)


@pytest.mark.asyncio
async def test_ringback_twiml_parks_the_person_for_the_original_rep_only():
    with patch(f"{_RINGBACK}.call_stages.offer_voicemail_ringback", new=AsyncMock(
             return_value={"inbound_call_id": "ic-1", "rep_user_id": _REP, "conference_name": f"call-{_CHILD}"})) as offer, \
         patch(f"{_RINGBACK}.notify_inbound_call") as notify:
        twiml = await dialer_voicemail_ringback.offer_ringback_twiml(
            parent_call_sid=_PARENT, prospect_call_sid=_CHILD, endpoint_root="https://api.example.com/api/v1/telephony",
        )
    offer.assert_awaited_once_with(provider="twilio", parent_call_sid=_PARENT,
                                   prospect_call_id=_CHILD, conference_name=f"call-{_CHILD}")
    notify.assert_called_once_with("ic-1")
    assert f">call-{_CHILD}</Conference>" in twiml
    assert 'startConferenceOnEnter="false"' in twiml and 'endConferenceOnExit="true"' in twiml
    assert "<Play>" not in twiml


@pytest.mark.asyncio
async def test_ringback_twiml_is_none_when_the_rep_is_not_free():
    with patch(f"{_RINGBACK}.call_stages.offer_voicemail_ringback", new=AsyncMock(return_value=None)), \
         patch(f"{_RINGBACK}.notify_inbound_call") as notify:
        assert await dialer_voicemail_ringback.offer_ringback_twiml(
            parent_call_sid=_PARENT, prospect_call_sid=_CHILD, endpoint_root="https://x",
        ) is None
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [True, False])
async def test_unanswered_ringback_lets_the_person_go_only_if_the_rep_never_took_it(expired):
    # The Phase 1b chain is unavailable here: the Phase 1 behaviour is the fallback.
    with patch(f"{_RINGBACK}.expire_unanswered_inbound_call", new=AsyncMock(return_value=expired)), \
         patch(f"{_RINGBACK}.dialer_extra_answer.continue_after_ringback", new=AsyncMock(return_value=None)), \
         patch(f"{_RINGBACK}.cancel_call", new=AsyncMock(return_value=True)) as cancel, \
         patch(f"{_RINGBACK}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)) as failed, \
         patch(f"{_RINGBACK}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        await dialer_voicemail_ringback.expire_ringback(
            parent_call_sid=_PARENT, prospect_call_sid=_CHILD, delay_seconds=0,
        )
    if expired:
        cancel.assert_awaited_once_with(call_sid=_CHILD)
        failed.assert_awaited_once_with(parent_call_sid=_PARENT, reason="person_answered")
        assert record.await_args.args[2]["ringback"] == "missed"
    else:
        cancel.assert_not_awaited()
        failed.assert_not_awaited()


# ── SignalWire: ring-back, beep, mailbox full ────────────────────────────────

def _sw_context(line_count: int = 1, leg: dict | None = None):
    return {
        "session": {"id": _SESSION, "status": "dialing", "rep_user_id": _REP, "rep_call_id": "browser-call",
                    "line_count": line_count, "conference_name": f"sysevo-par-{_SESSION}", "winner_leg_id": None},
        "leg": leg or {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "screening"},
    }


def _sw_request(amd_result: str):
    from api.tests.test_signalwire_dialer_routes import _authed
    return _authed({"call": {"call_id": "prospect-call", "amd_result": amd_result, "amd_ms": "900", "beep": "true"}},
                   query={"session_id": _SESSION, "leg_id": _SW_LEG})


@pytest.fixture
def _sw_env(monkeypatch):
    from api.tests.test_signalwire_dialer_routes import _SECRET
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)


@pytest.mark.asyncio
async def test_signalwire_person_after_the_hand_off_rings_the_original_rep_back(_sw_env, monkeypatch):
    monkeypatch.setattr(extra_answer, "AUTO_RECORDING_NOTICE", True)  # the notice's own mechanics
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    spawned = []
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context())), \
         patch(f"{_SW}.signalwire_parallel_store.leg_voicemail_requested", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.claim_winner", new=AsyncMock()) as winner, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()) as history, \
         patch(f"{_SW}.call_stages.offer_voicemail_ringback", new=AsyncMock(
             return_value={"inbound_call_id": "ic-1", "rep_user_id": _REP})) as offer, \
         patch(f"{_SW}.notify_inbound_call") as notify, \
         patch("api.services.telephony.dialer.extra_answer.clip_url", new=AsyncMock(return_value=None)) as clip, \
         patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)):
        response = await handle_sw_parallel_decision(_sw_request("human"))
    for coro in spawned:
        coro.close()
    document = json.loads(response.body)
    room = offer.await_args.kwargs["conference_name"]
    assert offer.await_args.kwargs["parent_call_sid"] == "browser-call"
    assert offer.await_args.kwargs["prospect_call_id"] == "prospect-call"
    # Phase 1b: the rep's hello clip (here none recorded → neutral words) while their
    # softphone auto-joins; never hold music, never the voicemail recording.
    assert document == {"sections": {"main": [
        {"play": {"url": "say:This call may be recorded and monitored for quality assurance."}},
        {"play": {"url": "say:Hi, one moment."}},
        {"join_conference": {"name": room, "start_on_enter": False, "end_on_exit": True}},
    ]}}
    assert clip.await_args.args == (_REP, "hello")
    winner.assert_not_awaited()
    notify.assert_called_once_with("ic-1")
    assert history.await_args.kwargs["status"] == "in-progress"


@pytest.mark.asyncio
async def test_signalwire_person_after_the_hand_off_with_a_busy_rep_is_hung_up(_sw_env):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    spawned = []
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context())), \
         patch(f"{_SW}.signalwire_parallel_store.leg_voicemail_requested", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_SW}.call_stages.offer_voicemail_ringback", new=AsyncMock(return_value=None)), \
         patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)):
        response = await handle_sw_parallel_decision(_sw_request("human"))
    names = [getattr(c, "__name__", "") for c in spawned]
    for coro in spawned:
        coro.close()
    assert json.loads(response.body) == {"sections": {"main": [{"hangup": {}}]}}
    assert "_record_stages" in names


@pytest.mark.asyncio
async def test_signalwire_mailbox_passed_to_the_rep_records_machine_then_beep(_sw_env):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    spawned = []
    policy = {"on_machine": "drop_vm", "single_auto_send": False, "parallel_auto_send": True, "voicemail_mode": "ask"}
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context())), \
         patch(f"{_SW}.signalwire_parallel_store.leg_voicemail_requested", new=AsyncMock(return_value=False)), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value=policy)), \
         patch(f"{_SW}.signalwire_parallel_store.claim_winner", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}._signalwire_parallel_join_swml", new=AsyncMock(return_value={"sections": {"main": [{"join_conference": {}}]}})), \
         patch(f"{_SW}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record, \
         patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)):
        await handle_sw_parallel_decision(_sw_request("machine"))
        assert len(spawned) == 1
        await spawned[0]
    assert [(c.args[0], c.args[1]) for c in record.await_args_list] == [
        ("browser-call", "machine"), ("browser-call", "beep"),
    ]


@pytest.mark.asyncio
async def test_signalwire_line_that_ends_seconds_into_the_message_is_mailbox_full(_sw_env):
    from datetime import datetime, timedelta, timezone
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_status
    from api.tests.test_signalwire_dialer_routes import _authed
    leg = {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "voicemail_playing",
           "voicemail_started_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    request = _authed({"call": {"call_id": "prospect-call", "call_state": "ended", "end_reason": "hangup"}},
                      query={"session_id": _SESSION, "leg_id": _SW_LEG})
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context(leg=leg))), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)) as update_leg, \
         patch(f"{_SW}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)) as outcome, \
         patch(f"{_SW}.signalwire_parallel_store.complete_voicemail_drop", new=AsyncMock()) as sent, \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_SW}._finish_signalwire_batch_if_settled", new=AsyncMock()), \
         patch(f"{_SW}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        response = await handle_sw_parallel_status(request)
    assert json.loads(response.body) == {"ok": True, "mailbox_full": True}
    assert update_leg.await_args.kwargs["status"] == "failed"
    assert outcome.await_args.kwargs["outcome"] == "mailbox_full"
    sent.assert_not_awaited()
    assert record.await_args.args[:2] == ("browser-call", "mailbox_full")


@pytest.mark.asyncio
async def test_signalwire_full_playback_records_vm_left(_sw_env):
    from datetime import datetime, timedelta, timezone
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_playback
    from api.tests.test_signalwire_dialer_routes import _authed
    leg = {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "voicemail_playing",
           "voicemail_started_at": (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()}
    request = _authed({"call_id": "prospect-call", "state": "finished"},
                      query={"session_id": _SESSION, "leg_id": _SW_LEG})
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context(leg=leg))), \
         patch(f"{_SW}.signalwire_parallel_store.complete_voicemail_drop", new=AsyncMock(return_value=True)) as sent, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_SW}._finish_signalwire_batch_if_settled", new=AsyncMock()), \
         patch(f"{_SW}.call_stages.record_call_stage", new=AsyncMock(return_value=True)) as record:
        await handle_sw_parallel_playback(request)
    sent.assert_awaited_once()
    record.assert_awaited_once_with("browser-call", "vm_left", {})


@pytest.mark.asyncio
async def test_signalwire_finished_seconds_after_starting_is_mailbox_full(_sw_env):
    from datetime import datetime, timedelta, timezone
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_playback
    from api.tests.test_signalwire_dialer_routes import _authed
    leg = {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "voicemail_playing",
           "voicemail_started_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    request = _authed({"call_id": "prospect-call", "state": "finished"},
                      query={"session_id": _SESSION, "leg_id": _SW_LEG})
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context(leg=leg))), \
         patch(f"{_SW}.signalwire_parallel_store.complete_voicemail_drop", new=AsyncMock()) as sent, \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)) as outcome, \
         patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_SW}._finish_signalwire_batch_if_settled", new=AsyncMock()), \
         patch(f"{_SW}.call_stages.record_call_stage", new=AsyncMock(return_value=True)):
        await handle_sw_parallel_playback(request)
    sent.assert_not_awaited()
    assert outcome.await_args.kwargs["outcome"] == "mailbox_full"


def test_ringback_wait_swml_refuses_other_rooms():
    with pytest.raises(ValueError):
        build_signalwire_ringback_wait_swml(conference_name="sysevo-par-x")
    doc = build_signalwire_ringback_wait_swml(conference_name="sysevo-ringback-1")
    assert doc["sections"]["main"][-1]["join_conference"] == {
        "name": "sysevo-ringback-1", "start_on_enter": False, "end_on_exit": True,
    }


@pytest.mark.asyncio
async def test_sorter_seam_routes_verdicts_through_the_listening_hook():
    from api.services.telephony.dialer import call_stage_events
    with patch(f"{_STAGES}.report_listening_stage", new=AsyncMock(return_value=True)) as hook, \
         patch(f"{_STAGES}.record_call_stage", new=AsyncMock(return_value=True)) as record:
        await call_stage_events.record_call_stage(_PARENT, "ivr", {"phrase": "press 1"})
        await call_stage_events.record_call_stage(_PARENT, "beep", {})
    hook.assert_awaited_once_with(call_key=_PARENT, stage="ivr", detail={"phrase": "press 1"})
    record.assert_awaited_once_with(_PARENT, "beep", {"source": "listening"})


def test_with_stage_passes_the_stage_detail_through():
    body = call_stages.with_stage(
        {"state": "idle"},
        {"stage": "agent_ivr", "stage_at": "t4", "detail": {"pressed": ["2", "5"]}},
    )
    assert body["stage_detail"] == {"pressed": ["2", "5"]}
