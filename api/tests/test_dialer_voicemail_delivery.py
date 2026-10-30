"""Voicemail delivery: playback completion evidence, AMD timing, parallel and
detached-rep paths, shared-library resolution for SignalWire.

Every provider and database boundary is mocked; no call is placed.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import unescape

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.providers.twilio import routes as twilio_routes
from api.services.telephony.providers.twilio.routes import router

_PATH = "api.services.telephony.providers.twilio.routes"
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


def _complete_url(twiml: str) -> dict[str, list[str]]:
    start = twiml.index("<Redirect")
    url = unescape(twiml[twiml.index(">", start) + 1: twiml.index("</Redirect>")])
    return parse_qs(urlparse(url).query)


# ── completion evidence ──────────────────────────────────────────────────────

def test_playback_twiml_signs_the_expected_duration_into_the_completion_url():
    from api.services.telephony.providers.twilio.dialer_call_log import remember_voicemail_duration
    remember_voicemail_duration(_DROP, 24)
    twiml = twilio_routes._voicemail_playback_twiml(
        endpoint_root="https://api.example.test/api/v1/telephony",
        parent_call_sid=_PARENT, playback_url=_URL, pause_seconds=6,
        parallel_leg_id=_LEG, drop_id=_DROP,
    )
    assert '<Pause length="6"/><Play>' in twiml
    query = _complete_url(twiml)
    assert query["parent_call_sid"] == [_PARENT]
    assert query["parallel_leg_id"] == [_LEG]
    assert query["vm_expect"] == ["30000"]  # 24 s recording + 6 s grace pause
    assert int(query["vm_started"][0]) <= int(datetime.now(UTC).timestamp() * 1000)


def test_playback_twiml_without_a_known_duration_keeps_the_old_contract():
    twiml = twilio_routes._voicemail_playback_twiml(
        endpoint_root="https://api.example.test", parent_call_sid=_PARENT, playback_url=_URL,
        drop_id="44444444-4444-4444-8444-444444444444",
    )
    assert "vm_expect" not in twiml and "<Pause" not in twiml


@pytest.mark.parametrize(("elapsed_ms", "expect_ms", "finished"), [
    (25_000, 24_000, True),
    (21_000, 24_000, True),    # inside the 15 % provider slack
    (1_200, 24_000, False),    # <Play> skipped an unplayable file
    (0, 0, True),              # no expectation signed
])
def test_playback_finished_uses_the_signed_expectation(elapsed_ms, expect_ms, finished):
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert twilio_routes._playback_finished(
        vm_started=str(now_ms - elapsed_ms) if expect_ms else "", vm_expect=str(expect_ms) if expect_ms else "",
    ) is finished


def test_early_completion_is_recorded_as_a_failed_playback_not_as_sent():
    started = int(datetime.now(UTC).timestamp() * 1000) - 900
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={"child_call_sid": _CHILD})), \
         patch(f"{_PATH}.mark_auto_voicemail_sent", new=AsyncMock()) as sent, \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)) as failed, \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as end_rep:
        response = _app().post(
            "/dialer-auto-voicemail-complete",
            params={"parent_call_sid": _PARENT, "vm_started": str(started), "vm_expect": "24000"},
            data={"CallSid": _CHILD},
            headers={"X-Twilio-Signature": "sig"},
        )
    assert response.status_code == 200 and "<Hangup/>" in response.text
    sent.assert_not_awaited()
    failed.assert_awaited_once_with(
        parent_call_sid=_PARENT, reason="playback_incomplete", child_call_sid=None, leg_id=None,
    )
    end_rep.assert_awaited_once_with(call_sid=_PARENT)


def test_full_length_completion_is_recorded_as_sent():
    started = int(datetime.now(UTC).timestamp() * 1000) - 25_000
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={"child_call_sid": _CHILD})), \
         patch(f"{_PATH}.mark_auto_voicemail_sent", new=AsyncMock()) as sent, \
         patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock()) as failed, \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()):
        _app().post(
            "/dialer-auto-voicemail-complete",
            params={"parent_call_sid": _PARENT, "vm_started": str(started), "vm_expect": "24000"},
            data={"CallSid": _CHILD},
            headers={"X-Twilio-Signature": "sig"},
        )
    sent.assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=None)
    failed.assert_not_awaited()


# ── AMD decision time ────────────────────────────────────────────────────────

def test_single_call_amd_decision_time_is_recorded():
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock(return_value=True)) as timing, \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})):
        response = _app().post(
            "/dialer-conference-join",
            params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT},
            data={"AnsweredBy": "human", "CallSid": _CHILD, "MachineDetectionDuration": "2410"},
            headers={"X-Twilio-Signature": "sig"},
        )
    assert response.status_code == 200 and "<Conference" in response.text
    timing.assert_awaited_once_with(parent_call_sid=_PARENT, decision_ms="2410")


# ── parallel machine legs ────────────────────────────────────────────────────

def test_parallel_machine_leg_uses_automatic_policy_even_if_the_rep_pressed_drop():
    """A manual press on the parallel aggregate used to make every machine
    leg try a MANUAL claim on a dialer_calls row that does not exist for that
    child, so no voicemail was left on any line."""
    with patch(f"{_PATH}.dialer_parallel.update_parallel_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock(return_value=True)) as timing, \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={
             "rep_user_id": _REP, "manual_voicemail_requested_at": "2026-10-19T00:00:00Z",
         })), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={
             "auto_drop_enabled": True, "on_machine": "drop_vm",
         })) as policy, \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, _URL, _DROP))) as settings, \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)) as claim, \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)):
        response = _app().post(
            "/dialer-conference-join",
            params={
                "conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT,
                "parallel_leg_id": _LEG, "parallel_entry_id": _ENTRY,
            },
            data={"AnsweredBy": "machine_end_beep", "CallSid": _CHILD, "MachineDetectionDuration": "3105"},
            headers={"X-Twilio-Signature": "sig"},
        )
    assert response.status_code == 200
    assert "<Play>" in response.text and "<Conference" not in response.text
    claim.assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD, manual=False)
    assert settings.await_args.kwargs["allow_manual_when_disabled"] is False
    assert settings.await_args.kwargs["entry_id"] == _ENTRY
    assert policy.await_args.kwargs["entry_id"] == _ENTRY
    query = _complete_url(response.text)
    assert query["parallel_leg_id"] == [_LEG]
    timing.assert_awaited_once_with(
        parent_call_sid=_PARENT, decision_ms="3105", child_call_sid=_CHILD, leg_id=_LEG,
    )


# ── queued manual drop survives the rep leaving ──────────────────────────────

@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_a_ringing_leg_with_a_queued_voicemail():
    leg = {
        "child_call_sid": _CHILD, "status": "ringing",
        "manual_voicemail_requested_at": "2026-10-19T00:00:00Z",
        "voicemail_drop_started_at": None, "auto_voicemail_sent": False,
    }
    with patch(f"{_PATH}.dialer_parallel.parallel_session", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=leg)), \
         patch(f"{_PATH}.mark_rep_detached_for_voicemail", new=AsyncMock(return_value=True)) as detach, \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as cancel:
        await twilio_routes._cancel_orphaned_lead_leg(_PARENT)
    detach.assert_awaited_once_with(parent_call_sid=_PARENT)
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_cleanup_still_cancels_when_the_queue_cannot_be_kept():
    leg = {"child_call_sid": _CHILD, "status": "ringing", "manual_voicemail_requested_at": "x"}
    with patch(f"{_PATH}.dialer_parallel.parallel_session", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=leg)), \
         patch(f"{_PATH}.mark_rep_detached_for_voicemail", new=AsyncMock(return_value=False)), \
         patch(f"{_PATH}.cancel_call", new=AsyncMock()) as cancel:
        await twilio_routes._cancel_orphaned_lead_leg(_PARENT)
    cancel.assert_awaited_once_with(call_sid=_CHILD)


@pytest.mark.parametrize("answered_by", ["human", "unknown", ""])
def test_person_answering_after_the_rep_left_is_never_put_in_an_empty_conference(answered_by):
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={
             "rep_detached_at": "2026-10-19T00:00:00Z",
         })):
        response = _app().post(
            "/dialer-conference-join",
            params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT},
            data={"AnsweredBy": answered_by, "CallSid": _CHILD},
            headers={"X-Twilio-Signature": "sig"},
        )
    assert "<Hangup/>" in response.text and "<Conference" not in response.text


def test_machine_answering_after_the_rep_left_still_receives_the_voicemail():
    with patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value={
             "rep_user_id": _REP, "manual_voicemail_requested_at": "2026-10-19T00:00:00Z",
         })), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": "drop_vm"})), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, _URL, _DROP))), \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)) as claim, \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={"rep_detached_at": "x"})):
        response = _app().post(
            "/dialer-conference-join",
            params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT},
            data={"AnsweredBy": "machine_end_beep", "CallSid": _CHILD},
            headers={"X-Twilio-Signature": "sig"},
        )
    assert "<Play>" in response.text
    claim.assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD, manual=True)


# ── manual drop on a live call with AMD off ──────────────────────────────────

def test_manual_drop_on_a_live_call_without_amd_plays_on_the_reps_press():
    row = {
        "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD,
        "amd_result": None, "manual_voicemail_requested_at": "2026-10-19T00:00:00Z",
        "voicemail_drop_started_at": None, "auto_voicemail_sent": False,
        "ended_at": None, "lead_joined_at": "2026-10-19T00:00:00Z",
    }
    twilio = MagicMock()
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, _URL, _DROP))), \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.request_manual_voicemail_drop", new=AsyncMock(return_value={"accepted": True, "state": "ready"})), \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}._twilio_client", new=AsyncMock(return_value=twilio)):
        response = _app(authenticated=True).post(
            "/dialer/manual-voicemail-drop", json={"parent_call_sid": _PARENT, "action": "send"},
        )
    assert response.json() == {"state": "playing", "released": True}
    twiml = twilio.calls.return_value.update.call_args.kwargs["twiml"]
    assert "<Pause" not in twiml and "<Play>" in twiml


def test_rep_press_on_a_live_line_plays_even_when_detection_said_human():
    """The rep hears a mailbox that Twilio's detection read as a person: their press wins."""
    for verdict in ("human", "unknown"):
        row = {
            "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD, "amd_result": verdict,
            "manual_voicemail_requested_at": "2026-10-19T00:00:00Z", "voicemail_drop_started_at": None,
            "auto_voicemail_sent": False, "voicemail_drop_failed_at": None,
            "ended_at": None, "lead_joined_at": "2026-10-19T00:00:00Z",
        }
        twilio = MagicMock()
        with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
             patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, _URL, _DROP))), \
             patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
             patch(f"{_PATH}.request_manual_voicemail_drop", new=AsyncMock(return_value={"accepted": True, "state": "ready"})), \
             patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
             patch(f"{_PATH}._twilio_client", new=AsyncMock(return_value=twilio)):
            response = _app(authenticated=True).post(
                "/dialer/manual-voicemail-drop", json={"parent_call_sid": _PARENT, "action": "send"},
            )
        assert response.json() == {"state": "playing", "released": True}, verdict
        assert "<Play>" in twilio.calls.return_value.update.call_args.kwargs["twiml"]


def test_manual_status_reports_needs_rep_and_failed_states():
    row = {
        "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD, "amd_result": None,
        "manual_voicemail_requested_at": "2026-10-19T00:00:00Z", "voicemail_drop_started_at": None,
        "auto_voicemail_sent": False, "lead_joined_at": "2026-10-19T00:00:01Z",
    }
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})):
        body = _app(authenticated=True).get(f"/dialer/manual-voicemail-drop/{_PARENT}").json()
    assert body["state"] == "needs_rep"

    row = {**row, "amd_result": "machine_end_beep", "voicemail_drop_started_at": "2026-10-19T00:00:02Z"}
    with patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={
             "voicemail_drop_failed_at": "2026-10-19T00:00:03Z", "voicemail_drop_failure": "playback_incomplete",
         })):
        body = _app(authenticated=True).get(f"/dialer/manual-voicemail-drop/{_PARENT}").json()
    assert body == {"state": "failed", "amd_result": "machine_end_beep", "ready": True, "failure": "playback_incomplete",
                    "stage": None, "stage_at": None, "stage_detail": {}}


# ── SignalWire: same resolver, no fax playback, sign before claim ────────────

_SW = "api.services.telephony.dialer.signalwire_routes"


def _sw_context(line_count=3):
    session_id = "00000000-0000-0000-0000-000000000021"
    return session_id, {
        "session": {"id": session_id, "status": "dialing", "rep_user_id": _REP, "rep_call_id": "browser",
                    "line_count": line_count, "conference_name": f"sysevo-par-{session_id}"},
        "leg": {"provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": "screening"},
    }


@pytest.fixture
def _sw_env(monkeypatch):
    from api.tests.test_signalwire_dialer_routes import _SECRET
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)
    yield


def _sw_request(amd_result: str, session_id: str):
    from api.tests.test_signalwire_dialer_routes import _authed
    return _authed(
        {"call": {"call_id": "prospect-call", "amd_result": amd_result, "amd_ms": "2650"}},
        query={"session_id": session_id, "leg_id": "00000000-0000-0000-0000-000000000022"},
    )


@pytest.mark.asyncio
async def test_signalwire_never_plays_a_voicemail_to_a_fax_line(_sw_env):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    session_id, context = _sw_context()
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value={
             "on_machine": "drop_vm", "auto_drop_enabled": True})), \
         patch(f"{_SW}._load_signalwire_parallel_default_drop", new=AsyncMock()) as load, \
         patch(f"{_SW}.signalwire_parallel_store.claim_voicemail_drop", new=AsyncMock()) as claim, \
         patch(f"{_SW}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)) as outcome, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock()) as amd:
        await handle_sw_parallel_decision(_sw_request("fax", session_id))
    load.assert_not_awaited()
    claim.assert_not_awaited()
    # The decision really ran: the fax is recorded and the leg finished.
    outcome.assert_awaited_once()
    assert amd.await_args.kwargs["amd_result"] == "fax"


@pytest.mark.asyncio
async def test_signalwire_signing_failure_does_not_strand_a_claimed_leg(_sw_env):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    session_id, context = _sw_context()
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)) as update_leg, \
         patch(f"{_SW}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value={
             "on_machine": "drop_vm", "auto_drop_enabled": True})), \
         patch(f"{_SW}._load_signalwire_parallel_default_drop", new=AsyncMock(return_value={
             "id": _DROP, "storage_path": f"{_REP}/m.wav"})) as load, \
         patch(f"{_SW}._signed_signalwire_voicemail_url", new=AsyncMock(side_effect=RuntimeError("storage down"))), \
         patch(f"{_SW}.signalwire_parallel_store.claim_voicemail_drop", new=AsyncMock()) as claim, \
         patch(f"{_SW}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)) as outcome, \
         patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock()):
        await handle_sw_parallel_decision(_sw_request("machine", session_id))
    load.assert_awaited_once_with(_REP, entry_id=_ENTRY, parent_call_sid="browser")
    claim.assert_not_awaited()
    outcome.assert_awaited_once()
    assert update_leg.await_args.kwargs["status"] == "voicemail"


@pytest.mark.asyncio
async def test_signalwire_default_drop_uses_the_shared_resolver_and_refuses_webm():
    from api.services.telephony.dialer import signalwire_routes
    with patch(f"{_SW}.SUPABASE_URL", "https://example.supabase.co"), \
         patch(f"{_SW}.SUPABASE_SERVICE_ROLE_KEY", "service"), \
         patch(f"{_SW}.resolve_effective_voicemail_drop", new=AsyncMock(return_value={
             "id": _DROP, "storage_path": "22222222-2222-4222-8222-222222222222/team.wav",
             "owner_user_id": "22222222-2222-4222-8222-222222222222", "source": "team_default"})) as resolve:
        drop = await signalwire_routes._load_signalwire_parallel_default_drop(_REP, entry_id=_ENTRY, parent_call_sid="browser")
    assert drop == {"id": _DROP, "storage_path": "22222222-2222-4222-8222-222222222222/team.wav"}
    resolve.assert_awaited_once_with(rep_user_id=_REP, entry_id=_ENTRY, parent_call_sid="browser")

    with patch(f"{_SW}.SUPABASE_URL", "https://example.supabase.co"), \
         patch(f"{_SW}.SUPABASE_SERVICE_ROLE_KEY", "service"), \
         patch(f"{_SW}.resolve_effective_voicemail_drop", new=AsyncMock(return_value={
             "id": _DROP, "storage_path": f"{_REP}/old.webm", "owner_user_id": _REP})):
        assert await signalwire_routes._load_signalwire_parallel_default_drop(_REP) is None
