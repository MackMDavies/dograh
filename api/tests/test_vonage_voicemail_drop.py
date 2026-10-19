"""Vonage sales-dialer prerecorded voicemail drop."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.telephony.providers.vonage import routes  # noqa: I001 - import first, as the app does
from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.dialer import vonage_voicemail as vm

_VM = "api.services.telephony.dialer.vonage_voicemail"
_ROUTES = "api.services.telephony.providers.vonage.routes"
SECRET = "sig-secret"
ROOT = "https://api.example.test/api/v1/telephony"
REP = str(uuid4())
CALL = str(uuid4())
CHILD = str(uuid4())
REP_LEG = str(uuid4())
DROP = str(uuid4())
WAV_URL = "https://supabase.example/storage/v1/object/sign/voicemail-drops/r/m.wav?token=t"


def _row(**overrides):
    row = {
        "parent_call_sid": CALL, "child_call_sid": CHILD, "rep_user_id": REP, "entry_id": None,
        "provider": "vonage", "from_number": "+14155550100", "to_number": "+14155552671",
        "status": "in-progress", "ended_at": None, "amd_result": None,
        "manual_voicemail_requested_at": None, "voicemail_drop_started_at": None,
        "auto_voicemail_sent": False, "voicemail_drop_id": None,
        "voicemail_drop_failed_at": None, "voicemail_drop_failure": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/telephony")
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(id=12, provider_id=REP)
    with (
        patch(f"{_ROUTES}._verify_dialer_webhook", new=AsyncMock(return_value={"api_key": "k"})),
        patch(f"{_ROUTES}._telephony_root", new=AsyncMock(return_value=ROOT)),
        patch(f"{_VM}.signature_secret", new=AsyncMock(return_value=SECRET)),
    ):
        yield TestClient(app)


# ─────────────────────────────── pure helpers ───────────────────────────────

def test_signed_context_rejects_any_tampering():
    query = parse_qs(vm.sign_context(SECRET, {"call": CALL, "rep": REP_LEG}))
    flat = {key: values[0] for key, values in query.items()}
    assert vm.verify_context(SECRET, flat) == {"call": CALL, "rep": REP_LEG}
    assert vm.verify_context(SECRET, {**flat, "call": str(uuid4())}) is None
    assert vm.verify_context("other-secret", flat) is None
    assert vm.verify_context(SECRET, {"call": CALL}) is None


def test_payload_hash_binds_the_body_when_vonage_sends_one():
    body = b'{"status":"machine"}'
    assert vm.payload_hash_matches({"payload_hash": hashlib.sha256(body).hexdigest()}, body)
    assert not vm.payload_hash_matches({"payload_hash": hashlib.sha256(b"{}").hexdigest()}, body)
    assert vm.payload_hash_matches({}, body)


@pytest.mark.parametrize(
    ("status", "sub_state", "expected"),
    [
        ("human", None, "human"),
        ("machine", None, "machine_start"),
        ("machine", "beep_start", "machine_end_beep"),
        ("machine", "beep_timeout", "machine_end_other"),
        ("answered", None, None),
    ],
)
def test_vonage_amd_maps_onto_dialer_results(status, sub_state, expected):
    assert vm.classify_amd_event(status, sub_state) == expected


def test_playback_ncco_streams_once_then_reports_and_ends():
    ncco = vm.playback_ncco(playback_url=WAV_URL, complete_url="https://cb", dialer_call_id=CALL)
    assert ncco[0] == {"action": "stream", "streamUrl": [WAV_URL], "bargeIn": False, "loop": 1}
    assert ncco[1]["action"] == "notify" and ncco[1]["eventUrl"] == ["https://cb"]
    assert len(ncco) == 2  # nothing after notify: Vonage hangs the leg up


def test_playback_finished_uses_signed_expectation():
    now = int(time.time() * 1000)
    assert vm.playback_finished({"vm_started": str(now - 20_000), "vm_expect": "20000"})
    assert not vm.playback_finished({"vm_started": str(now - 2_000), "vm_expect": "20000"})
    assert vm.playback_finished({})


async def test_webm_recording_is_never_played():
    with patch(
        f"{_VM}.dialer_call_log.get_auto_voicemail_settings",
        new=AsyncMock(return_value=(True, WAV_URL.replace(".wav", ".webm"), DROP)),
    ):
        assert await vm.resolve_playback(rep_user_id=REP, dialer_call_id=CALL, entry_id=None) is None


async def test_claim_is_one_conditional_update_with_machine_guard():
    with patch(f"{_VM}._conditional_patch", new=AsyncMock(return_value=1)) as patch_rows:
        assert await vm.claim_playback(dialer_call_id=CALL, child_uuid=CHILD, drop_id=DROP, manual=False)
    filters, payload = patch_rows.await_args.args
    assert filters["voicemail_drop_started_at"] == "is.null"
    assert filters["auto_voicemail_sent"] == "is.false"
    assert filters["voicemail_drop_failed_at"] == "is.null"
    assert filters["ended_at"] == "is.null"
    assert filters["child_call_sid"] == f"eq.{CHILD}"
    assert filters["manual_voicemail_requested_at"] == "is.null"
    assert filters["amd_result"] == "in.(machine_end_beep,machine_end_other,machine_end_silence)"
    assert payload["voicemail_drop_id"] == DROP


async def test_auto_send_uses_the_shared_per_rep_resolver():
    single = {"single_auto_send": False, "auto_drop_enabled": True, "on_machine": "drop_vm"}
    assert not await vm.rep_auto_sends(rep_user_id=REP, dialer_call_id=CALL, entry_id=None, policy=single)
    single["single_auto_send"] = True
    assert await vm.rep_auto_sends(rep_user_id=REP, dialer_call_id=CALL, entry_id=None, policy=single)
    # Pre-migration policy: the team switch decides.
    assert await vm.rep_auto_sends(
        rep_user_id=REP, dialer_call_id=CALL, entry_id=None,
        policy={"auto_drop_enabled": True, "on_machine": "drop_vm"},
    )
    # A team action other than Drop VM never plays automatically.
    assert not await vm.rep_auto_sends(
        rep_user_id=REP, dialer_call_id=CALL, entry_id=None,
        policy={"single_auto_send": True, "auto_drop_enabled": True, "on_machine": "pass_to_rep"},
    )
    assert not await vm.rep_auto_sends(rep_user_id=REP, dialer_call_id=CALL, entry_id=None, policy=None)


# ─────────────────────────────── answer webhook ───────────────────────────────

def _answer_payload(**custom):
    return {
        "uuid": REP_LEG, "from_user": "rep-12",
        "custom_data": json.dumps({"to": "+14155552671", "from": "+14155550100", "dialer_call_id": CALL, **custom}),
    }


def test_answer_adds_signed_events_and_beep_detection(client):
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(child_call_sid=None, status="initiated"))),
        patch(f"{_VM}.dialer_call_log.get_team_voicemail_policy",
              new=AsyncMock(return_value={"auto_drop_enabled": True, "machine_detection_enabled": True})),
    ):
        response = client.get("/api/v1/telephony/vonage/dialer/answer", params=_answer_payload())
    assert response.status_code == 200
    connect = response.json()[0]
    assert connect["endpoint"] == [{"type": "phone", "number": "14155552671"}]
    assert connect["advancedMachineDetection"] == {"behavior": "continue", "mode": "detect_beep", "beep_timeout": 45}
    event_url = urlparse(connect["eventUrl"][0])
    assert event_url.path.endswith("/vonage/dialer/events")
    context = vm.verify_context(SECRET, {k: v[0] for k, v in parse_qs(event_url.query).items()})
    assert context == {"call": CALL, "rep": REP_LEG}


def test_answer_post_json_is_also_accepted(client):
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(child_call_sid=None))),
        patch(f"{_VM}.dialer_call_log.get_team_voicemail_policy", new=AsyncMock(return_value=None)),
    ):
        response = client.post("/api/v1/telephony/vonage/dialer/answer", json=_answer_payload())
    assert response.status_code == 200
    connect = response.json()[0]
    assert "advancedMachineDetection" not in connect  # team runs no detection
    assert connect["eventUrl"]


def test_answer_for_a_mismatched_history_row_keeps_the_plain_connect(client):
    with patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(child_call_sid=None, to_number="+14155559999"))):
        response = client.get("/api/v1/telephony/vonage/dialer/answer", params=_answer_payload())
    assert response.status_code == 200
    assert set(response.json()[0]) == {"action", "from", "endpoint"}


# ─────────────────────────────── leg events ───────────────────────────────

def _events_url(**fields):
    return "/api/v1/telephony/vonage/dialer/events?" + vm.sign_context(SECRET, fields or {"call": CALL, "rep": REP_LEG})


def test_events_with_a_forged_context_are_refused(client):
    response = client.post(
        "/api/v1/telephony/vonage/dialer/events?call=" + CALL + "&rep=" + REP_LEG + "&sig=bad",
        json={"uuid": CHILD, "status": "machine", "sub_state": "beep_start"},
    )
    assert response.status_code == 403


def test_beep_plays_the_recording_and_releases_the_rep(client):
    row = _row(amd_result="machine_end_beep")
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=row)),
        patch(f"{_VM}.bind_child_leg", new=AsyncMock(return_value=True)),
        patch(f"{_VM}.record_amd_result", new=AsyncMock(return_value=True)) as record,
        patch(f"{_VM}.dialer_call_log.get_team_voicemail_policy",
              new=AsyncMock(return_value={"auto_drop_enabled": True, "on_machine": "drop_vm"})),
        patch(f"{_VM}.dialer_call_log.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, WAV_URL, DROP))),
        patch(f"{_VM}.dialer_call_log.voicemail_duration", return_value=20.0),
        patch(f"{_VM}.claim_playback", new=AsyncMock(return_value=True)) as claim,
        patch(f"{_VM}.transfer_to_ncco", new=AsyncMock(return_value=True)) as transfer,
        patch(f"{_VM}.hangup_leg", new=AsyncMock(return_value=True)) as hangup,
    ):
        response = client.post(_events_url(), json={"uuid": CHILD, "status": "machine", "sub_state": "beep_start"})
    assert response.json() == {"status": "playing"}
    assert record.await_args.kwargs["amd_result"] == "machine_end_beep"
    assert claim.await_args.kwargs["manual"] is False
    uuid, ncco = transfer.await_args.args
    assert uuid == CHILD
    assert ncco[0]["streamUrl"] == [WAV_URL]
    complete = urlparse(ncco[1]["eventUrl"][0])
    signed = vm.verify_context(SECRET, {k: v[0] for k, v in parse_qs(complete.query).items()})
    assert signed["call"] == CALL and signed["leg"] == CHILD and signed["vm_expect"] == "20000"
    hangup.assert_awaited_once_with(REP_LEG)


def test_human_answer_never_plays(client):
    with (
        patch(f"{_VM}.bind_child_leg", new=AsyncMock(return_value=True)),
        patch(f"{_VM}.record_amd_result", new=AsyncMock(return_value=True)),
        patch(f"{_VM}.on_machine_end", new=AsyncMock()) as machine_end,
        patch(f"{_VM}.transfer_to_ncco", new=AsyncMock()) as transfer,
    ):
        response = client.post(_events_url(), json={"uuid": CHILD, "status": "human"})
    assert response.json() == {"status": "ok"}
    machine_end.assert_not_awaited()
    transfer.assert_not_awaited()


def test_machine_greeting_start_waits_for_the_beep(client):
    with (
        patch(f"{_VM}.bind_child_leg", new=AsyncMock(return_value=True)),
        patch(f"{_VM}.record_amd_result", new=AsyncMock(return_value=True)) as record,
        patch(f"{_VM}.on_machine_end", new=AsyncMock()) as machine_end,
    ):
        client.post(_events_url(), json={"uuid": CHILD, "status": "machine"})
    assert record.await_args.kwargs["amd_result"] == "machine_start"
    machine_end.assert_not_awaited()


def test_event_for_an_unrelated_leg_is_ignored(client):
    with (
        patch(f"{_VM}.bind_child_leg", new=AsyncMock(return_value=False)),
        patch(f"{_VM}.record_amd_result", new=AsyncMock()) as record,
    ):
        response = client.post(_events_url(), json={"uuid": str(uuid4()), "status": "machine", "sub_state": "beep_start"})
    assert response.json() == {"status": "ignored"}
    record.assert_not_awaited()


async def test_one_tap_rep_hears_the_mailbox_when_auto_is_off():
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(amd_result="machine_end_beep"))),
        patch(f"{_VM}.dialer_call_log.get_team_voicemail_policy",
              new=AsyncMock(return_value={"auto_drop_enabled": False, "on_machine": "drop_vm"})),
        patch(f"{_VM}.start_playback", new=AsyncMock()) as start,
        patch(f"{_VM}.hangup_leg", new=AsyncMock()) as hangup,
    ):
        result = await vm.on_machine_end(
            endpoint_root=ROOT, secret=SECRET, dialer_call_id=CALL, child_uuid=CHILD, rep_leg_uuid=REP_LEG,
        )
    assert result == "rep_decides"
    start.assert_not_awaited()
    hangup.assert_not_awaited()


async def test_team_hangup_policy_ends_the_machine_leg():
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(amd_result="machine_end_beep"))),
        patch(f"{_VM}.dialer_call_log.get_team_voicemail_policy",
              new=AsyncMock(return_value={"auto_drop_enabled": False, "on_machine": "hangup"})),
        patch(f"{_VM}.hangup_leg", new=AsyncMock(return_value=True)) as hangup,
    ):
        result = await vm.on_machine_end(
            endpoint_root=ROOT, secret=SECRET, dialer_call_id=CALL, child_uuid=CHILD, rep_leg_uuid=REP_LEG,
        )
    assert result == "hangup"
    hangup.assert_awaited_once_with(CHILD)


async def test_lost_claim_neither_transfers_nor_releases():
    with (
        patch(f"{_VM}.dialer_call_log.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, WAV_URL, DROP))),
        patch(f"{_VM}.claim_playback", new=AsyncMock(return_value=False)),
        patch(f"{_VM}.transfer_to_ncco", new=AsyncMock()) as transfer,
        patch(f"{_VM}.hangup_leg", new=AsyncMock()) as hangup,
    ):
        result = await vm.start_playback(
            endpoint_root=ROOT, secret=SECRET, row=_row(), child_uuid=CHILD, rep_leg_uuid=REP_LEG, manual=False,
        )
    assert result == "not_claimed"
    transfer.assert_not_awaited()
    hangup.assert_not_awaited()


async def test_refused_transfer_is_recorded_and_keeps_the_rep_connected():
    with (
        patch(f"{_VM}.dialer_call_log.get_auto_voicemail_settings", new=AsyncMock(return_value=(True, WAV_URL, DROP))),
        patch(f"{_VM}.claim_playback", new=AsyncMock(return_value=True)),
        patch(f"{_VM}.transfer_to_ncco", new=AsyncMock(return_value=False)),
        patch(f"{_VM}.mark_failed", new=AsyncMock(return_value=True)) as failed,
        patch(f"{_VM}.hangup_leg", new=AsyncMock()) as hangup,
    ):
        result = await vm.start_playback(
            endpoint_root=ROOT, secret=SECRET, row=_row(), child_uuid=CHILD, rep_leg_uuid=REP_LEG, manual=False,
        )
    assert result == "failed"
    assert failed.await_args.kwargs["reason"] == "provider_transfer_failed"
    hangup.assert_not_awaited()


# ─────────────────────────────── completion ───────────────────────────────

def _complete_url(started_ms_ago: int, expect_ms: int = 20_000, leg: str = CHILD):
    started = int(time.time() * 1000) - started_ms_ago
    return "/api/v1/telephony/vonage/dialer/voicemail-complete?" + vm.sign_context(
        SECRET, {"call": CALL, "leg": leg, "vm_started": str(started), "vm_expect": str(expect_ms)},
    )


def test_full_playback_is_recorded_as_sent(client):
    with (
        patch(f"{_VM}.mark_sent", new=AsyncMock(return_value=True)) as sent,
        patch(f"{_VM}.mark_failed", new=AsyncMock()) as failed,
    ):
        response = client.post(_complete_url(21_000), json={"uuid": CHILD, "dialer_call_id": CALL})
    assert response.status_code == 204
    sent.assert_awaited_once_with(dialer_call_id=CALL, child_uuid=CHILD)
    failed.assert_not_awaited()


def test_early_completion_is_a_failure_not_a_sent(client):
    with (
        patch(f"{_VM}.mark_sent", new=AsyncMock()) as sent,
        patch(f"{_VM}.mark_failed", new=AsyncMock(return_value=True)) as failed,
    ):
        response = client.post(_complete_url(1_000), json={"uuid": CHILD})
    assert response.status_code == 204
    sent.assert_not_awaited()
    assert failed.await_args.kwargs["reason"] == "playback_incomplete"


def test_completion_for_another_leg_is_refused(client):
    with patch(f"{_VM}.mark_sent", new=AsyncMock()) as sent:
        response = client.post(_complete_url(21_000), json={"uuid": str(uuid4())})
    assert response.status_code == 403
    sent.assert_not_awaited()


async def test_leg_ending_mid_message_records_failure_and_after_it_records_sent():
    started = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
    row = _row(voicemail_drop_started_at=started, voicemail_drop_id=DROP)
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=row)),
        patch(f"{_VM}.dialer_call_log.voicemail_duration", return_value=20.0),
        patch(f"{_VM}.mark_failed", new=AsyncMock(return_value=True)) as failed,
    ):
        assert await vm.on_leg_ended(dialer_call_id=CALL, child_uuid=CHILD) == "failed"
    assert failed.await_args.kwargs["reason"] == "call_ended_during_playback"

    row["voicemail_drop_started_at"] = (datetime.now(UTC) - timedelta(seconds=25)).isoformat()
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=row)),
        patch(f"{_VM}.dialer_call_log.voicemail_duration", return_value=20.0),
        patch(f"{_VM}.mark_sent", new=AsyncMock(return_value=True)) as sent,
    ):
        assert await vm.on_leg_ended(dialer_call_id=CALL, child_uuid=CHILD) == "sent"
    sent.assert_awaited_once()


# ─────────────────────────────── one-tap ───────────────────────────────

def test_one_tap_on_a_confirmed_mailbox_plays_now(client):
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(amd_result="machine_end_beep"))),
        patch(f"{_VM}.request_manual_drop", new=AsyncMock(return_value=True)),
        patch(f"{_VM}.find_other_leg", new=AsyncMock(return_value=REP_LEG)),
        patch(f"{_VM}.start_playback", new=AsyncMock(return_value="playing")) as start,
    ):
        response = client.post(f"/api/v1/telephony/dialer/vonage/calls/{CALL}/voicemail-drop")
    assert response.json() == {"state": "playing"}
    assert start.await_args.kwargs["manual"] is True
    assert start.await_args.kwargs["require_machine_end"] is True
    assert start.await_args.kwargs["rep_leg_uuid"] == REP_LEG


def test_one_tap_on_a_human_is_refused(client):
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(amd_result="human"))),
        patch(f"{_VM}.start_playback", new=AsyncMock()) as start,
    ):
        response = client.post(f"/api/v1/telephony/dialer/vonage/calls/{CALL}/voicemail-drop")
    assert response.status_code == 409
    start.assert_not_awaited()


def test_one_tap_during_the_greeting_waits_for_the_beep(client):
    with (
        patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(amd_result="machine_start"))),
        patch(f"{_VM}.request_manual_drop", new=AsyncMock(return_value=True)) as request,
        patch(f"{_VM}.start_playback", new=AsyncMock()) as start,
    ):
        response = client.post(f"/api/v1/telephony/dialer/vonage/calls/{CALL}/voicemail-drop")
    assert response.json() == {"state": "waiting"}
    request.assert_awaited_once()
    start.assert_not_awaited()


def test_one_tap_on_another_reps_call_is_not_found(client):
    with patch(f"{_VM}.get_call", new=AsyncMock(return_value=_row(rep_user_id=str(uuid4())))):
        response = client.post(f"/api/v1/telephony/dialer/vonage/calls/{CALL}/voicemail-drop")
    assert response.status_code == 404
