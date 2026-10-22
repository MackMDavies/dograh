import json
import xml.dom.minidom
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.providers.twilio.routes import _parallel_batch_has_settled, router

_DISCLOSURE = "<Say>This call may be recorded and monitored for quality assurance.</Say>"

# A real-shaped Twilio Call SID ("CA" + 32 lowercase hex). Listen-in
# validates this shape before the value becomes a conference name, so the
# short "CA111" placeholder the other tests use is deliberately rejected
# there - see test_dialer_listen_connect_rejects_malformed_parent_call_sid.
_LISTEN_SID = "CA" + "abcdef0123456789" * 2
_MANAGER_LISTEN_SID = "CA" + "2" * 32


@pytest.fixture(autouse=True)
def _isolated_floor_leave_boundary(monkeypatch):
    # Ordinary rep/prospect events have no Floor session. Dedicated tests below
    # override this transport boundary to exercise manager closure and retry.
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.routes.close_floor_monitor_for_provider",
        AsyncMock(return_value=False),
    )


@pytest.mark.parametrize("status", [
    "completed", "busy", "no-answer", "failed", "cancelled", "abandoned_early",
])
def test_parallel_batch_considers_early_abandonment_settled(status):
    assert _parallel_batch_has_settled([{"status": status}]) is True


def test_parallel_batch_waits_for_voicemail_playback_and_live_lines():
    assert _parallel_batch_has_settled([]) is False
    assert _parallel_batch_has_settled([{"status": "voicemail", "auto_voicemail_sent": False}]) is False
    assert _parallel_batch_has_settled([{"status": "ringing"}]) is False
    assert _parallel_batch_has_settled([{"status": "voicemail", "auto_voicemail_sent": True}]) is True


# A resolvable dialer rep: voice-connect refuses an outbound dial whose identity does
# not resolve to a dialer user (it would skip the recording lock and call history).
_DIALER_REP = SimpleNamespace(id=7, provider_id="10000000-0000-0000-0000-000000000007")


@pytest.fixture(autouse=True)
def _dialer_rep_defaults():
    """Defaults for a resolved rep; tests that care patch these themselves."""
    path = "api.services.telephony.providers.twilio.routes"
    with patch(f"{path}.call_stages.voicemail_recording_required", new=AsyncMock(return_value=False)), \
         patch(f"{path}.create_dialer_call", new=AsyncMock(return_value=True)), \
         patch(f"{path}.get_team_voicemail_policy", new=AsyncMock(return_value=None)):
        yield


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _make_authenticated_test_app():
    app = _make_test_app()
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(
        provider_id="11111111-1111-1111-1111-111111111111"
    )
    return app


@pytest.mark.parametrize('result,status', [('changed', 200), ('busy', 409), ('unavailable', 404), ('failed', 503)])
def test_floor_mode_change_binds_authenticated_actor(result, status):
    session_id = '20000000-0000-0000-0000-000000000001'
    with patch('api.services.telephony.providers.twilio.routes.change_floor_monitor_mode', new_callable=AsyncMock, return_value=result) as change:
        response = TestClient(_make_authenticated_test_app()).post('/dialer/floor-monitor/mode', json={
            'session_id': session_id, 'mode': 'whisper', 'manager_user_id': 'untrusted-body-actor',
        })
    assert response.status_code == status
    change.assert_awaited_once_with(session_id=session_id, manager_user_id='11111111-1111-1111-1111-111111111111', mode='whisper')
    if status == 200:
        assert response.json() == {'session_id': session_id, 'mode': 'whisper'}


@pytest.mark.parametrize('body', [
    {'session_id': 'bad', 'mode': 'listen'},
    {'session_id': '20000000-0000-0000-0000-000000000001', 'mode': 'unknown'},
])
def test_floor_mode_change_validates_request_before_provider(body):
    with patch('api.services.telephony.providers.twilio.routes.change_floor_monitor_mode', new_callable=AsyncMock) as change:
        response = TestClient(_make_authenticated_test_app()).post('/dialer/floor-monitor/mode', json=body)
    assert response.status_code == 422
    change.assert_not_awaited()


@pytest.fixture(autouse=True)
def allow_unblocked_test_phones(monkeypatch):
    # Tests that exercise the provider webhook do not use live Supabase. By
    # default, model a successful server-side compliance lookup; individual
    # tests can override it to prove the fail-closed path.
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.dialer_agent_handoff.record_handoff_event", AsyncMock(return_value=False))
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.dialer_hold.hold_twiml", AsyncMock(return_value=None))
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.dialer_hold.independent_children", AsyncMock(return_value=set()))
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.dialer_hold.record_ended", AsyncMock())
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.dialer_overflow_recovery.schedule_overflow", AsyncMock(return_value=True))
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.extra_answer.parallel_line_cap", AsyncMock(return_value=None))
    monkeypatch.setattr("api.services.telephony.providers.twilio.routes.extra_answer.mark", AsyncMock(return_value=None))
    with patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_phone_block_reason",
        return_value=None,
    ):
        async def hours_open(_phone, _entry_id=None):
            return True, None

        async def no_phone_block(_phone):
            return None

        async def candidate_phones(entry_ids):
            return {entry_id: f"+1555{index:07d}" for index, entry_id in enumerate(entry_ids, start=1)}

        monkeypatch.setattr(
            "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours",
            hours_open,
        )
        monkeypatch.setattr(
            "api.services.telephony.providers.twilio.routes.phone_block_reason",
            no_phone_block,
        )
        async def hours_open_with_state(_phone, _entry_id=None):
            return True, None, "CA"

        monkeypatch.setattr(
            "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours_with_state",
            hours_open_with_state,
        )
        monkeypatch.setattr(
            "api.services.telephony.providers.twilio.routes.dialer_parallel.candidate_phones",
            candidate_phones,
        )
        yield


@pytest.fixture(autouse=True)
def automatic_voicemail_is_opted_out_by_default(monkeypatch):
    async def opted_out(*, user_id: str, **_kwargs):
        return False, None, None

    async def default_team_policy(*, user_id: str, **_kwargs):
        return {
            "auto_drop_enabled": False,
            "machine_detection_enabled": True,
            "machine_detection_preset": "balanced",
            "on_machine": "drop_vm",
        }

    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        opted_out,
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.routes.get_team_voicemail_policy",
        default_team_policy,
    )

    async def auth_token():
        return "test-auth-token"

    async def caller_id():
        return "+15551234567"

    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.routes._resolve_dialer_auth_token",
        auth_token,
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.routes._resolve_dialer_caller_id",
        caller_id,
    )


def test_voice_connect_returns_conference_twiml_for_valid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ):
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert "call-CA111</Conference>" in response.text
    assert 'record="record-from-start"' in response.text
    assert 'startConferenceOnEnter="true"' in response.text
    assert 'endConferenceOnExit="true"' in response.text
    assert 'beep="false"' in response.text
    assert 'statusCallbackEvent="start end join leave"' in response.text
    assert (
        'statusCallback="https://api.example.com/api/v1/telephony/dialer-conference-events"'
        in response.text
    )
    assert (
        'recordingStatusCallback="https://api.example.com/api/v1/telephony/dialer-recording-callback"'
        in response.text
    )
    # The disclosure belongs on the LEAD's leg (dialer-conference-join), not
    # the rep's - playing it here would both delay the rep joining and mean
    # the called party never hears it.
    assert _DISCLOSURE not in response.text


def test_voice_connect_joins_only_a_conference_claimed_by_this_rep(monkeypatch):
    parent_sid = "CA" + "abcdef0123456789" * 2
    conference_name = f"call-{parent_sid}"
    client = TestClient(_make_test_app())
    rep = SimpleNamespace(provider_id="11111111-1111-1111-1111-111111111111")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        new=AsyncMock(return_value=rep),
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_claimed_inbound_conference",
        new=AsyncMock(return_value=True),
    ) as claim, patch(
        "api.services.telephony.providers.twilio.routes.bind_inbound_rep_leg",
        new=AsyncMock(return_value=True),
    ) as bind, patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        new=AsyncMock(return_value=("https://api.example.com", "wss://api.example.com")),
    ):
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-123", "To": conference_name, "conference": conference_name,
                  "CallSid": "CA" + "1" * 32},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert f"<{conference_name}" not in response.text
    assert f">{conference_name}</Conference>" in response.text
    assert _DISCLOSURE in response.text
    claim.assert_awaited_once_with(
        conference_name=conference_name,
        rep_user_id=rep.provider_id,
    )
    bind.assert_awaited_once_with(
        conference_name=conference_name, rep_user_id=rep.provider_id,
        provider="twilio", rep_call_id="CA" + "1" * 32,
    )


def test_voice_connect_rejects_unclaimed_inbound_conference(monkeypatch):
    parent_sid = "CA" + "abcdef0123456789" * 2
    conference_name = f"call-{parent_sid}"
    client = TestClient(_make_test_app())
    rep = SimpleNamespace(provider_id="11111111-1111-1111-1111-111111111111")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        new=AsyncMock(return_value=rep),
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_claimed_inbound_conference",
        new=AsyncMock(return_value=False),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as dial_lead:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-123", "To": conference_name, "conference": conference_name},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    dial_lead.assert_not_called()


def test_voice_connect_hangs_up_on_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text


def test_voice_connect_hangs_up_when_to_number_missing(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/voice-connect",
            data={"CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text


def test_voice_connect_hangs_up_when_call_sid_missing(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text


def test_voice_connect_parallel_fans_out_only_server_validated_targets(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    targets = [
        {"leg_id": "11111111-1111-4111-8111-111111111111", "entry_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "phone": "+15550000001", "business": "One"},
        {"leg_id": "22222222-2222-4222-8222-222222222222", "entry_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "phone": "+15550000002", "business": "Two"},
    ]

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id", return_value=None
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=SimpleNamespace(provider_id="aaaaaaaa-0000-4000-8000-000000000001"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.start_parallel_session",
        return_value=True,
    ) as start_session, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_targets",
        return_value=targets,
    ), patch(
        "api.services.telephony.providers.twilio.routes.parallel_caller_id_candidates",
        return_value=["+15551234567", "+15557654321"],
    ), patch(
        "api.services.telephony.providers.twilio.routes.choose_parallel_caller_ids",
        return_value={
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": "+15557654321",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": "+15551234567",
        },
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.assign_parallel_caller_id",
        return_value=True,
    ) as persist_caller_id, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.register_parallel_leg",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.update_parallel_leg",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
        return_value=True,
    ) as create_call, patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        side_effect=["CA" + "1" * 32, "CA" + "2" * 32],
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559999999",
                "CallSid": _LISTEN_SID,
                "From": "client:rep-42",
                "DialMode": "parallel",
                "ParallelLineCount": "2",
                "ParallelEntryIds": '["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]',
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert start_session.call_args.kwargs["entry_ids"] == [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ]
    assert originate.call_count == 2
    assert {call.kwargs["lead_number"] for call in originate.call_args_list} == {
        "+15550000001", "+15550000002"
    }
    assert all(call.kwargs["machine_detection"] for call in originate.call_args_list)
    assert {call.kwargs["caller_id"] for call in originate.call_args_list} == {
        "+15551234567", "+15557654321"
    }
    assert persist_caller_id.await_count == 2
    assert create_call.await_args.kwargs["from_number"] == "+15557654321"
    assert create_call.await_args.kwargs["entry_id"] == targets[0]["entry_id"]


def test_voice_connect_parallel_persists_caller_ids_only_after_the_aggregate_row_exists(monkeypatch):
    """dialer_parallel_assign_caller_id matches the dialer_calls row for the
    parent call SID; called before that row exists it updates nothing and the
    whole batch is refused. Model that contract instead of mocking the two
    calls independently (which hid the ordering bug)."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    targets = [
        {"leg_id": "11111111-1111-4111-8111-111111111111", "entry_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "phone": "+15550000001", "business": "One"},
        {"leg_id": "22222222-2222-4222-8222-222222222222", "entry_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "phone": "+15550000002", "business": "Two"},
    ]
    rows: list[dict] = []
    order: list[str] = []

    async def create_row(**kwargs):
        order.append("create_row")
        rows.append(kwargs)
        return True

    async def assign(**kwargs):
        order.append("assign")
        return any(row["parent_call_sid"] == kwargs["parent_call_sid"] for row in rows)

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id", return_value=None
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=SimpleNamespace(provider_id="aaaaaaaa-0000-4000-8000-000000000001"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.start_parallel_session",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_targets",
        return_value=targets,
    ), patch(
        "api.services.telephony.providers.twilio.routes.parallel_caller_id_candidates",
        return_value=["+15551234567"],
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.assign_parallel_caller_id",
        side_effect=assign,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.finish_parallel_session",
        return_value=True,
    ) as finish, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.register_parallel_leg",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
        side_effect=create_row,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        side_effect=["CA" + "1" * 32, "CA" + "2" * 32],
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559999999",
                "CallSid": _LISTEN_SID,
                "From": "client:rep-42",
                "DialMode": "parallel",
                "ParallelLineCount": "2",
                "ParallelEntryIds": '["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]',
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" not in response.text
    assert "<Conference" in response.text
    assert order == ["create_row", "assign", "assign"]
    finish.assert_not_awaited()
    assert originate.call_count == 2
    assert {call.kwargs["caller_id"] for call in originate.call_args_list} == {"+15551234567"}


def test_voice_connect_parallel_screens_every_destination_against_do_not_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    checked: list[str] = []

    async def second_line_blocked(phone):
        checked.append(phone)
        return "do_not_call" if phone == "+15550000002" else None

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id", return_value=None
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_phone_block_reason",
        side_effect=second_line_blocked,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.candidate_phones",
        return_value={
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": "+15550000001",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": "+15550000002",
        },
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.start_parallel_session",
    ) as start_session, patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ) as create_call, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15550000001",
                "CallSid": _LISTEN_SID,
                "From": "client:rep-42",
                "DialMode": "parallel",
                "ParallelLineCount": "2",
                "ParallelEntryIds": '["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]',
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    # The DNC lead is dropped; one lead is not a batch, so nothing dials and the rep is told.
    assert "Fewer than two leads" in response.text
    assert sorted(checked) == ["+15550000001", "+15550000002"]
    start_session.assert_not_awaited()
    create_call.assert_not_awaited()
    originate.assert_not_awaited()


def test_voice_connect_blocks_suppressed_number_before_creating_or_originating_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_phone_block_reason",
        return_value="do_not_call",
    ) as compliance, patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ) as create_call, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": _LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    compliance.assert_awaited_once_with("+15559876543")
    create_call.assert_not_awaited()
    originate.assert_not_awaited()


def test_voice_connect_blocks_outside_calling_hours_before_creating_or_originating_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours_with_state",
        return_value=(False, "outside_calling_hours", "CA"),
    ) as hours_check, patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ) as create_call, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": _LISTEN_SID, "EntryId": "entry-123"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    # The rep hears why instead of the browser phone's bare "ConnectionError 31005".
    assert "outside calling hours" in response.text
    hours_check.assert_awaited_once_with("+15559876543", "entry-123")
    create_call.assert_not_awaited()
    originate.assert_not_awaited()


def test_voice_connect_call_anyway_overrides_calling_hours_but_not_do_not_call(monkeypatch):
    """The rep confirmed "Call anyway": time-of-day reasons give way, the DNC register never."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours_with_state",
        return_value=(False, "outside_calling_hours", "CA"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_phone_block_reason", return_value=None,
    ):
        refused = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": _LISTEN_SID, "EntryId": "entry-123", "HoursOverride": "1"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )
    # Past the calling-hours gate: the next refusal is the unresolved identity, not the hours.
    assert "outside calling hours" not in refused.text

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_phone_block_reason", return_value="do_not_call",
    ), patch(
        "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours_with_state",
    ) as hours_check:
        dnc = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": _LISTEN_SID, "EntryId": "entry-123", "HoursOverride": "1"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )
    assert "do not call list" in dnc.text and "<Hangup/>" in dnc.text
    hours_check.assert_not_awaited()


def test_rep_overrides_hours_only_for_time_of_day_reasons():
    from api.services.telephony.dialer.compliance import rep_overrides_hours

    assert rep_overrides_hours("outside_calling_hours", "1")
    assert rep_overrides_hours("public_holiday", "true")
    assert rep_overrides_hours("timezone_unavailable", True)
    assert not rep_overrides_hours("outside_calling_hours", "")
    assert not rep_overrides_hours("do_not_call", "1")
    assert not rep_overrides_hours("lead_phone_mismatch", "1")
    # The two-step warning also shows when the hours could not be confirmed.
    assert rep_overrides_hours("compliance_unavailable", "1")


def test_voice_connect_blocks_parallel_batch_if_any_number_is_outside_hours(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())

    async def one_closed(phone, _entry_id=None):
        return (phone != "+15550000002", None if phone != "+15550000002" else "outside_calling_hours")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id", return_value=None
    ), patch(
        "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours", side_effect=one_closed
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.candidate_phones",
        return_value={
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": "+15550000001",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": "+15550000002",
        },
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.start_parallel_session",
    ) as start_session, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559999999",
                "CallSid": _LISTEN_SID,
                "From": "client:rep-42",
                "DialMode": "parallel",
                "ParallelLineCount": "2",
                "ParallelEntryIds": '["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]',
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    start_session.assert_not_awaited()
    originate.assert_not_awaited()


def test_voice_connect_never_fans_out_when_server_compliance_guard_rejects_batch(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id", return_value=None
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=SimpleNamespace(provider_id="aaaaaaaa-0000-4000-8000-000000000001"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.start_parallel_session",
        return_value=False,
    ) as start_session, patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ) as create_call, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559999999",
                "CallSid": _LISTEN_SID,
                "From": "client:rep-42",
                "DialMode": "parallel",
                "ParallelLineCount": "2",
                "ParallelEntryIds": '["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]',
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    start_session.assert_awaited_once()
    create_call.assert_not_awaited()
    originate.assert_not_awaited()


def test_voice_connect_rejects_ten_line_batch_when_server_reservation_is_denied(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    entries = [f"{index:08x}-1111-4111-8111-{index:012x}" for index in range(1, 11)]
    candidates = {entry_id: f"+1555000{index:04d}" for index, entry_id in enumerate(entries, 1)}

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id", return_value=None
    ), patch(
        "api.services.telephony.providers.twilio.routes.check_dialer_calling_hours",
        return_value=(True, None),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.candidate_phones",
        return_value=candidates,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=SimpleNamespace(provider_id="11111111-1111-4111-8111-111111111111"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.start_parallel_session",
        return_value=False,
    ) as start_session, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559999999",
                "CallSid": _LISTEN_SID,
                "From": "client:rep-42",
                "DialMode": "parallel",
                "ParallelLineCount": "10",
                "ParallelEntryIds": json.dumps(entries),
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    start_session.assert_awaited_once()
    assert start_session.await_args.kwargs["line_count"] == 10
    originate.assert_not_awaited()


@pytest.mark.parametrize("amd_answer", ["human", "unknown"])
def test_parallel_join_bridges_only_atomic_human_winner(monkeypatch, amd_answer):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    parent_sid = _LISTEN_SID
    winner_sid = "CA" + "1" * 32
    loser_sid = "CA" + "2" * 32
    leg_id = "11111111-1111-4111-8111-111111111111"
    entry_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.claim_parallel_winner",
        return_value=True,
    ) as claim, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_legs",
        return_value=[
            {"child_call_sid": winner_sid, "status": "answered"},
            {"child_call_sid": loser_sid, "status": "ringing"},
        ],
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call_if_ringing", return_value=True
    ) as cancel:
        response = client.post(
            "/dialer-conference-join",
            params={
                "conference_name": f"call-{parent_sid}",
                "parent_call_sid": parent_sid,
                "parallel_leg_id": leg_id,
                "parallel_entry_id": entry_id,
                "events_url": "https://api.example.com/events",
                "recording_url": "https://api.example.com/recording",
            },
            data={"CallSid": winner_sid, "To": "+15550000001", "AnsweredBy": amd_answer},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    claim.assert_awaited_once()
    assert _DISCLOSURE in response.text
    assert "<Conference" in response.text
    # The accepted human winner must receive a valid conference-join document;
    # malformed TwiML here would leave the rep waiting with no connected lead.
    conference_doc = xml.dom.minidom.parseString(response.text)
    assert len(conference_doc.getElementsByTagName("Conference")) == 1
    assert len(conference_doc.getElementsByTagName("Dial")) == 1
    assert len(conference_doc.getElementsByTagName("Hangup")) == 0
    claim.assert_awaited_once_with(
        parent_call_sid=parent_sid,
        entry_id=entry_id,
        child_call_sid=winner_sid,
        leg_id=leg_id,
    )
    cancel.assert_awaited_once_with(call_sid=loser_sid)


_XA = "api.services.telephony.dialer.extra_answer"
_TXA = "api.services.telephony.providers.twilio.dialer_extra_answer"
_EXTRA_CHILD = "CA" + "2" * 32
_EXTRA_LEG = "11111111-1111-4111-8111-111111111111"
_EXTRA_REP = "44444444-4444-4444-8444-444444444444"


def _post_second_human(amd_answer="human", account=""):
    return TestClient(_make_test_app()).post(
        "/dialer-conference-join",
        params={
            "conference_name": f"call-{_LISTEN_SID}",
            "parent_call_sid": _LISTEN_SID,
            "parallel_leg_id": _EXTRA_LEG,
            "parallel_entry_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
        data={"CallSid": _EXTRA_CHILD, "AnsweredBy": amd_answer, "AccountSid": account},
        headers={"X-Twilio-Signature": "fake-signature"},
    )


def _chain_patches(*, advance, begin=None, clip=None):
    path = "api.services.telephony.providers.twilio.routes"
    return (
        patch(f"{path}.RequestValidator.validate", return_value=True),
        patch(f"{path}.dialer_parallel.claim_parallel_winner", new=AsyncMock(return_value=False)),
        patch(f"{path}.dialer_parallel.route_parallel_to_teammate", new=AsyncMock()),
        patch(f"{path}.dialer_hold.hold_twiml", new=AsyncMock()),
        patch(f"{_XA}.begin", new=AsyncMock(return_value=begin if begin is not None else {
            "status": "active", "step": "start", "rep_user_id": _EXTRA_REP})),
        patch(f"{_XA}.advance", new=AsyncMock(side_effect=advance)),
        patch(f"{_XA}.clip_url", new=AsyncMock(return_value=clip)),
    )


@pytest.mark.parametrize("amd_answer", ["human", "unknown"])
def test_second_human_auto_connects_a_teammate_and_is_never_held(monkeypatch, amd_answer):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    advance = [{"action": "teammate", "prospect_call_id": _EXTRA_CHILD, "rep_user_id": _EXTRA_REP,
                "inbound_call_id": "inbound-1", "conference_name": f"call-{_EXTRA_CHILD}"}]
    p = _chain_patches(advance=advance)
    with p[0], p[1], p[2] as old_route, p[3] as hold, p[4] as begin, p[5] as adv, p[6], \
            patch(f"{_TXA}.notify_inbound_call") as notify, patch(f"{_TXA}._spawn") as spawn:
        response = _post_second_human(amd_answer)
    for call in spawn.call_args_list:
        call.args[0].close()
    assert response.status_code == 200
    assert "<Hangup/>" not in response.text
    assert _DISCLOSURE in response.text
    doc = xml.dom.minidom.parseString(response.text)
    conference = doc.getElementsByTagName("Conference")[0]
    assert conference.firstChild.data == f"call-{_EXTRA_CHILD}"
    assert conference.getAttribute("startConferenceOnEnter") == "false"
    # Never Twilio's hold music: the wait plays the rep's hello clip.
    assert conference.getAttribute("waitUrl").endswith(f"/dialer-extra-answer-wait?call={_EXTRA_CHILD}")
    assert begin.await_args.kwargs["source"] == "parallel"
    assert begin.await_args.kwargs["leg_id"] == _EXTRA_LEG
    # The person hears the recording notice first: the offer runs ~3 s after it.
    assert adv.await_args.kwargs == {"provider": "twilio", "prospect_call_id": _EXTRA_CHILD, "offer_seconds": 7}
    notify.assert_called_once_with("inbound-1")
    assert spawn.call_count == 1   # the 8 s offer expiry
    old_route.assert_not_awaited()
    hold.assert_not_awaited()


def test_second_human_with_nobody_free_hears_the_reps_apology(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    advance = [{"action": "apology", "prospect_call_id": _EXTRA_CHILD, "rep_user_id": _EXTRA_REP}]
    p = _chain_patches(advance=advance, clip="https://storage.example.com/sorry.wav")
    with p[0], p[1], p[2], p[3] as hold, p[4], p[5], p[6] as clip:
        response = _post_second_human()
    assert response.status_code == 200
    assert "<Play>https://storage.example.com/sorry.wav</Play><Hangup/>" in response.text
    assert clip.await_args.args == (_EXTRA_REP, "apology")
    hold.assert_not_awaited()


def test_second_human_apology_falls_back_to_neutral_speech(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    p = _chain_patches(advance=[{"action": "apology", "prospect_call_id": _EXTRA_CHILD, "rep_user_id": _EXTRA_REP}])
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        response = _post_second_human()
    assert "<Say>Sorry, I've just been pulled onto another call." in response.text
    assert response.text.endswith("<Hangup/></Response>")


def test_second_human_with_apology_switched_off_is_ended_without_hold(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    p = _chain_patches(advance=[{"action": "hangup", "prospect_call_id": _EXTRA_CHILD}])
    with p[0], p[1], p[2], p[3] as hold, p[4], p[5], p[6]:
        response = _post_second_human()
    # Apology off still ends politely: a spoken goodbye, never a bare hang-up.
    assert response.text == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        "<Say>Sorry we missed you, we'll call you back shortly. Bye for now.</Say><Hangup/></Response>"
    )
    hold.assert_not_awaited()


def test_second_human_goes_to_the_voice_agent_on_the_same_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    account = "AC" + "3" * 32
    prepared = SimpleNamespace(twiml='<Response><Connect><Stream url="wss://example.invalid/media"/></Connect></Response>',
                               already_streaming=False)
    p = _chain_patches(advance=[{"action": "agent", "prospect_call_id": _EXTRA_CHILD, "rep_user_id": _EXTRA_REP}])
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6], patch(
        f"{_TXA}.dialer_agent_handoff.prepare_extra_answer_agent", new=AsyncMock(return_value=prepared),
    ) as adopt:
        response = _post_second_human(account=account)
    assert response.text == prepared.twiml
    assert adopt.await_args.kwargs == {"prospect_call_sid": _EXTRA_CHILD, "account_sid": account, "disclose": True}


def test_second_human_skips_an_unavailable_agent_to_the_next_step(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    from api.services.telephony.providers.twilio.dialer_agent_handoff import AgentHandoffUnavailable
    advance = [
        {"action": "agent", "prospect_call_id": _EXTRA_CHILD, "rep_user_id": _EXTRA_REP},
        {"action": "apology", "prospect_call_id": _EXTRA_CHILD, "rep_user_id": _EXTRA_REP},
    ]
    p = _chain_patches(advance=advance)
    with p[0], p[1], p[2], p[3], p[4], p[5] as adv, p[6], patch(
        f"{_TXA}.dialer_agent_handoff.prepare_extra_answer_agent",
        new=AsyncMock(side_effect=AgentHandoffUnavailable("no quota")),
    ):
        response = _post_second_human(account="AC" + "3" * 32)
    assert adv.await_args_list[1].kwargs == {"provider": "twilio", "prospect_call_id": _EXTRA_CHILD, "after": "agent"}
    assert "Sorry, I've just been pulled onto another call." in response.text


def test_second_human_is_apologised_to_when_the_chain_is_unavailable(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    p = _chain_patches(advance=[], begin={})
    with p[0], p[1], p[2], p[3] as hold, p[4], p[5] as adv, p[6]:
        response = _post_second_human()
    assert "Sorry, I've just been pulled onto another call." in response.text
    adv.assert_not_awaited()
    hold.assert_not_awaited()


def test_retried_second_human_webhook_repeats_the_reserved_step(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    p = _chain_patches(advance=[], begin={"status": "active", "step": "teammate", "rep_user_id": _EXTRA_REP})
    with p[0], p[1], p[2], p[3], p[4], p[5] as adv, p[6]:
        response = _post_second_human()
    adv.assert_not_awaited()   # never reserves a second teammate
    assert f"call-{_EXTRA_CHILD}</Conference>" in response.text


def test_agent_stream_callback_uses_iso_provider_timestamp(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    path = "api.services.telephony.providers.twilio.routes"
    with patch(f"{path}.RequestValidator.validate",return_value=True),patch(
        f"{path}.dialer_agent_handoff.record_handoff_event",new=AsyncMock(return_value=True),
    ) as record:
        result = TestClient(_make_test_app()).post("/dialer-agent-stream-status?workflow_run_id=501",data={
            "CallSid":_LISTEN_SID,"StreamEvent":"stream-started","Timestamp":"2026-10-18T12:00:00.123Z",
        },headers={"X-Twilio-Signature":"fake-signature"})
    assert result.status_code == 200
    record.assert_awaited_once_with(call_id=_LISTEN_SID,event="started",event_at="2026-10-18T12:00:00.123000+00:00",workflow_run_id="501")


def test_signed_hold_callback_resolves_exact_existing_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN","test-auth-token")
    path = "api.services.telephony.providers.twilio.routes"
    leg = "11111111-1111-4111-8111-111111111111"
    with patch(f"{path}.RequestValidator.validate",return_value=True),patch(
        f"{path}.dialer_hold.hold_twiml",new=AsyncMock(return_value="<Response><Pause length=\"1\"/></Response>"),
    ) as resolve:
        response = TestClient(_make_test_app()).post("/dialer-parallel-hold-next",params={"parent_call_sid":_LISTEN_SID,"leg_id":leg},
            data={"CallSid":"CA"+"2"*32,"AccountSid":"AC"+"3"*32},headers={"X-Twilio-Signature":"fake-signature"})
    assert response.status_code == 200
    resolve.assert_awaited_once_with(parent_call_sid=_LISTEN_SID,child_call_sid="CA"+"2"*32,leg_id=leg,account_sid="AC"+"3"*32)


def test_unsigned_hold_callback_is_rejected(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN","test-auth-token")
    response = TestClient(_make_test_app()).post("/dialer-parallel-hold-next",params={
        "parent_call_sid":_LISTEN_SID,"leg_id":"11111111-1111-4111-8111-111111111111"},data={"CallSid":"CA"+"2"*32})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_parent_teardown_preserves_independent_agent_and_hold_calls():
    from api.services.telephony.providers.twilio.routes import _cancel_orphaned_lead_leg
    path = "api.services.telephony.providers.twilio.routes"
    with (
        patch(f"{path}.dialer_parallel.parallel_session",new=AsyncMock(return_value={"winning_child_call_sid":"winner"})),
        patch(f"{path}.dialer_parallel.parallel_legs",new=AsyncMock(return_value=[
            {"child_call_sid":"winner","status":"answered"},
            {"child_call_sid":"agent","status":"answered"},
            {"child_call_sid":"held","status":"answered"},
            {"child_call_sid":"ringing","status":"ringing"},
        ])),
        patch(f"{path}.dialer_hold.independent_children",new=AsyncMock(return_value={"agent","held"})),
        patch(f"{path}.get_dialer_call_child_leg",new=AsyncMock(return_value=None)),
        patch(f"{path}.cancel_call_if_ringing",new=AsyncMock()) as cancel,
    ):
        await _cancel_orphaned_lead_leg(_LISTEN_SID)
    cancel.assert_awaited_once_with(call_sid="ringing")


@pytest.mark.asyncio
async def test_unclaimed_twilio_offer_falls_back_to_agent_after_eight_seconds():
    from api.services.telephony.providers.twilio.routes import _expire_parallel_overflow_offer
    path = "api.services.telephony.providers.twilio.routes"
    with (
        patch(f"{path}.asyncio.sleep",new=AsyncMock()) as sleep,
        patch(f"{path}.dialer_overflow_recovery.recover_overflow_offers",new=AsyncMock()) as recover,
    ):
        await _expire_parallel_overflow_offer(child_call_sid=_LISTEN_SID,parent_call_sid="parent",leg_id="leg",account_sid="account")
    sleep.assert_awaited_once_with(8)
    recover.assert_awaited_once_with(child_call_sid=_LISTEN_SID)


@pytest.mark.asyncio
async def test_overflow_timer_defers_transient_failure_to_durable_worker():
    from api.services.telephony.providers.twilio.routes import _expire_parallel_overflow_offer
    path = "api.services.telephony.providers.twilio.routes"
    with (
        patch(f"{path}.asyncio.sleep",new=AsyncMock()),
        patch(f"{path}.dialer_overflow_recovery.recover_overflow_offers",new=AsyncMock(side_effect=RuntimeError("storage unavailable"))),
        patch(f"{path}.cancel_call",new=AsyncMock()) as cancel,
    ):
        await _expire_parallel_overflow_offer(child_call_sid=_LISTEN_SID,parent_call_sid="parent",leg_id="leg",account_sid="account")
    cancel.assert_not_awaited()


def test_voice_connect_uses_assigned_number_for_recognized_rep(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value="+15559998888",
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as mock_dial:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_dial.assert_awaited_once_with(
        parent_call_sid="CA111",
        lead_number="+15559876543",
        caller_id="+15559998888",
        join_conference_url=(
            "https://api.example.com/api/v1/telephony/dialer-conference-join"
            "?conference_name=call-CA111&muted=false&end_on_exit=true&start_on_enter=true"
            "&events_url=https%3A%2F%2Fapi.example.com%2Fapi%2Fv1%2Ftelephony%2F"
            "dialer-conference-events"
            "&recording_url=https%3A%2F%2Fapi.example.com%2Fapi%2Fv1%2Ftelephony%2F"
            "dialer-recording-callback&parent_call_sid=CA111"
        ),
        status_callback_url=(
            "https://api.example.com/api/v1/telephony/dialer-call-status?parent_call_sid=CA111"
        ),
        machine_detection=True,
        machine_detection_preset="balanced",
        ringing_timeout_seconds=30,
        recording_callback_url="https://api.example.com/api/v1/telephony/dialer-recording-callback",
    )


def test_voice_connect_enables_amd_only_for_opted_in_rep_with_saved_drop(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    user_id = "00000000-0000-0000-0000-000000000001"
    fake_user = MagicMock(provider_id=user_id)
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value="+15559998888",
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        return_value=(True, "https://db.example.com/message.wav?token=x", "drop-1"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ) as dial:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111", "From": "client:rep-42"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    dial.assert_awaited_once()
    assert dial.await_args.kwargs["machine_detection"] is True


def test_voice_connect_falls_back_to_default_when_no_assignment(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as mock_dial:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_dial.assert_awaited_once()
    assert mock_dial.await_args.kwargs["caller_id"] == "+15551234567"


def test_voice_connect_hangs_up_when_lead_dial_fails(monkeypatch):
    """dial_lead_into_conference returning None means no lead leg exists at
    all, so the rep must not be dropped into an empty, silent conference."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value=None,
    ):
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Say>We could not connect that call. Please try again.</Say>" in response.text
    assert "<Conference" not in response.text


def test_voice_connect_creates_dialer_call_for_recognized_rep(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ), patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ) as mock_create:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559876543",
                "From": "client:rep-42",
                "CallSid": "CA111",
                "EntryId": "entry-abc",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_create.assert_awaited_once_with(
        parent_call_sid="CA111",
        rep_user_id="00000000-0000-0000-0000-000000000001",
        entry_id="entry-abc",
        from_number="+15551234567",
        to_number="+15559876543",
        phone_us_state="CA",
        double_dial_retry_of=None,
        double_dial_authorization_id=None,
    )


def test_voice_connect_refuses_to_originate_when_database_reservation_fails(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
        return_value=False,
    ) as reserve, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as originate:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559876543",
                "From": "client:rep-42",
                "CallSid": "CA111",
                "EntryId": "entry-abc",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "We could not verify this call" in response.text
    assert "<Hangup/>" in response.text
    reserve.assert_awaited_once()
    originate.assert_not_awaited()


def test_voice_connect_still_dials_when_get_backend_endpoints_raises(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        side_effect=Exception("backend unreachable"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ):
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text


def test_dialer_call_status_updates_on_valid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as mock_update:
        response = client.post(
            "/dialer-call-status?parent_call_sid=CA111",
            data={"CallSid": "CA222", "CallStatus": "completed", "CallDuration": "42"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_awaited_once_with(
        parent_call_sid="CA111",
        child_call_sid="CA222",
        status="completed",
        duration_seconds=42,
    )


def test_dialer_call_status_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as mock_update:
        response = client.post(
            "/dialer-call-status?parent_call_sid=CA111",
            data={"CallSid": "CA222", "CallStatus": "completed"},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert response.status_code == 401
    mock_update.assert_not_awaited()


def test_dialer_call_status_handles_missing_duration(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as mock_update:
        response = client.post(
            "/dialer-call-status?parent_call_sid=CA111",
            data={"CallSid": "CA222", "CallStatus": "ringing"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_awaited_once_with(
        parent_call_sid="CA111", child_call_sid="CA222", status="ringing", duration_seconds=None
    )


def test_parallel_status_marks_machine_answer_as_terminal_before_twiml_finishes(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    leg_id = "11111111-1111-4111-8111-111111111111"
    client = TestClient(_make_test_app())
    legs = [
        {"leg_id": leg_id, "child_call_sid": "CA" + "1" * 32, "status": "machine"},
        {"leg_id": "22222222-2222-4222-8222-222222222222", "child_call_sid": "CA" + "2" * 32, "status": "ringing"},
    ]
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.update_parallel_leg",
    ) as update_leg, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_legs",
        return_value=legs,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_session",
        return_value={"winning_child_call_sid": None},
    ):
        response = client.post(
            f"/dialer-call-status?parent_call_sid={parent_sid}&parallel_leg_id={leg_id}",
            data={
                "CallSid": "CA" + "1" * 32,
                "CallStatus": "completed",
                "AnsweredBy": "machine_end_beep",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert [call.kwargs for call in update_leg.await_args_list] == [
        {
            "child_call_sid": "CA" + "1" * 32,
            "leg_id": leg_id,
            "status": "machine",
            "answered_by": "machine_end_beep",
        },
        {
            "child_call_sid": "CA" + "1" * 32,
            "leg_id": leg_id,
            "status": "completed",
            "answered_by": "machine_end_beep",
        },
    ]


def test_parallel_answer_persists_signed_provider_timestamp(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    child_sid = "CA" + "1" * 32
    leg_id = "11111111-1111-4111-8111-111111111111"
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.record_parallel_timing",
        new_callable=AsyncMock,
    ) as record_timing, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.update_parallel_leg",
        new_callable=AsyncMock,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_legs",
        return_value=[{"status": "ringing"}],
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_session",
        return_value={"winning_child_call_sid": None},
    ):
        response = client.post(
            f"/dialer-call-status?parent_call_sid={parent_sid}&parallel_leg_id={leg_id}",
            data={
                "CallSid": child_sid,
                "CallStatus": "in-progress",
                "Timestamp": "Mon, 18 Oct 2026 12:34:56 +0000",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    record_timing.assert_awaited_once_with(
        parent_call_sid=parent_sid,
        child_call_sid=child_sid,
        event_kind="answer",
        event_at="2026-10-18T12:34:56+00:00",
        leg_id=leg_id,
    )


def test_twilio_event_time_rejects_missing_or_invalid_timestamp():
    from api.services.telephony.providers.twilio.routes import _twilio_event_time

    assert _twilio_event_time(None) is None
    assert _twilio_event_time("not a timestamp") is None


def test_dialer_call_status_ignores_missing_parent_call_sid(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as mock_update:
        response = client.post(
            "/dialer-call-status",
            data={"CallSid": "CA222", "CallStatus": "ringing"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_not_awaited()


def test_dialer_recording_callback_updates_on_valid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"ConferenceSid": "CF999", "RecordingSid": "RE999", "RecordingStatus": "completed"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_awaited_once_with(conference_sid="CF999", recording_sid="RE999")


def test_dialer_recording_callback_attaches_orphaned_recording_by_conference_name(monkeypatch):
    """conference-start was missed, so no row has the conference_sid: the
    recording is attached by the conference's name (call-<parent>)."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    parent = "CA" + "0" * 32

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
        AsyncMock(return_value=False),
    ), patch(
        "api.services.telephony.providers.twilio.routes._parent_call_sid_for_conference",
        AsyncMock(return_value=parent),
    ) as mock_lookup, patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_parent_call_sid",
        AsyncMock(return_value=True),
    ) as mock_fallback:
        response = client.post(
            "/dialer-recording-callback",
            data={"ConferenceSid": "CF999", "RecordingSid": "RE999", "RecordingStatus": "completed"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_lookup.assert_awaited_once_with("CF999")
    mock_fallback.assert_awaited_once_with(
        parent_call_sid=parent, conference_sid="CF999", recording_sid="RE999",
    )


def test_dialer_recording_callback_skips_fallback_when_conference_matched(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
        AsyncMock(return_value=True),
    ), patch(
        "api.services.telephony.providers.twilio.routes._parent_call_sid_for_conference",
        AsyncMock(),
    ) as mock_lookup:
        response = client.post(
            "/dialer-recording-callback",
            data={"ConferenceSid": "CF999", "RecordingSid": "RE999", "RecordingStatus": "completed"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_lookup.assert_not_awaited()


def test_dialer_recording_callback_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"ConferenceSid": "CF999", "RecordingSid": "RE999"},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert response.status_code == 401
    mock_update.assert_not_awaited()


def test_dialer_recording_callback_handles_missing_fields(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"ConferenceSid": "CF999"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    # Pin the reason, not just the 200: without this, this test and the
    # not-completed one below assert the identical pair of facts and the two
    # branches could be swapped with both still passing.
    assert response.json()["reason"] == "missing_fields"
    mock_update.assert_not_awaited()


def test_dialer_recording_callback_handles_missing_conference_sid(monkeypatch):
    """The realistic production shape of the missing-field case: a legacy
    <Dial record> callback (RecordingSid, CallSid, no ConferenceSid) landing
    from a call that started before this deploy."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"CallSid": "CA111", "RecordingSid": "RE999", "RecordingStatus": "completed"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert response.json()["reason"] == "missing_fields"
    mock_update.assert_not_awaited()


def test_dialer_recording_callback_attaches_a_voicemail_leg_recording(monkeypatch):
    """A voicemail plays on the prospect's own leg, outside the recorded
    conference: that leg's recording (CallSid, no ConferenceSid) is the call's."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    leg = "CA" + "3" * 32

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_child_call_sid",
        AsyncMock(return_value=True),
    ) as by_leg, patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
    ) as by_conference:
        response = client.post(
            "/dialer-recording-callback",
            data={"CallSid": leg, "RecordingSid": "RE999", "RecordingStatus": "completed"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.json() == {"status": "success", "source": "voicemail_leg"}
    by_leg.assert_awaited_once_with(child_call_sid=leg, recording_sid="RE999")
    by_conference.assert_not_awaited()


def test_dialer_recording_callback_ignores_non_completed_status(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording_by_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"ConferenceSid": "CF999", "RecordingSid": "RE999", "RecordingStatus": "in-progress"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert response.json()["reason"] == "recording_not_completed"
    mock_update.assert_not_awaited()


def test_dialer_conference_join_returns_conference_twiml(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert "call-CA111</Conference>" in response.text
    assert 'muted="false"' in response.text
    # Fail-safe default: with endConferenceOnExit="false" a lead hanging up
    # would leave the rep alone in a live conference, so the Voice SDK never
    # fires `disconnect` and the rep's UI hangs with a running timer.
    assert 'endConferenceOnExit="true"' in response.text
    assert 'startConferenceOnEnter="true"' in response.text
    assert 'beep="false"' in response.text
    # This is the lead's leg, so this is where the called party hears the
    # recording/monitoring disclosure.
    assert _DISCLOSURE in response.text
    assert response.text.index(_DISCLOSURE) < response.text.index("<Dial>")


def test_dialer_conference_join_plays_saved_voicemail_after_confirmed_beep(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"rep_user_id": "11111111-1111-1111-1111-111111111111"},
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        return_value=(True, "https://db.example.com/storage/v1/object/sign/voicemail-drops/u/message.wav?token=x", "drop-1"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_manual_voicemail_playback",
        return_value=True,
    ):
        response = client.post(
            f"/dialer-conference-join?conference_name=call-{parent_sid}&parent_call_sid={parent_sid}",
            data={"AnsweredBy": "machine_end_beep", "CallSid": parent_sid},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Play>https://db.example.com/storage/v1/object/sign/voicemail-drops/u/message.wav?token=x</Play>" in response.text
    assert "dialer-auto-voicemail-complete" in response.text
    assert "<Conference" not in response.text


@pytest.mark.parametrize("amd_result", ["machine_end_silence", "machine_end_other"])
def test_dialer_conference_join_waits_six_seconds_after_machine_greeting_without_beep(monkeypatch, amd_result):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"rep_user_id": "11111111-1111-1111-1111-111111111111"},
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        return_value=(True, "https://db.example.com/storage/v1/object/sign/voicemail-drops/u/message.wav?token=x", "drop-1"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_manual_voicemail_playback",
        return_value=True,
    ):
        response = client.post(
            f"/dialer-conference-join?conference_name=call-{parent_sid}&parent_call_sid={parent_sid}",
            data={"AnsweredBy": amd_result, "CallSid": parent_sid},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    # Playback starts immediately: a pause only records silence into the mailbox.
    assert '<Pause' not in response.text
    assert '<Play>https://db.example.com/' in response.text
    assert "dialer-auto-voicemail-complete" in response.text
    assert "<Conference" not in response.text


def test_twilio_machine_end_other_can_pass_to_rep_when_manager_selects_training_mode(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"rep_user_id": "11111111-1111-1111-1111-111111111111"},
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_team_voicemail_policy",
        return_value={"on_machine": "pass_to_rep"},
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        return_value=(False, None, None),
    ):
        response = client.post(
            f"/dialer-conference-join?conference_name=call-{parent_sid}&parent_call_sid={parent_sid}",
            data={"AnsweredBy": "machine_end_other", "CallSid": parent_sid},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert "dialer-auto-voicemail-complete" not in response.text


def test_manual_voicemail_waits_for_signed_amd_result(monkeypatch):
    parent_sid = _LISTEN_SID
    child_sid = "CA" + "1234567890abcdef" * 2
    row = {
        "rep_user_id": "11111111-1111-1111-1111-111111111111",
        "provider": "twilio",
        "child_call_sid": child_sid,
        "amd_result": None,
        "manual_voicemail_requested_at": None,
        "voicemail_drop_started_at": None,
        "auto_voicemail_sent": False,
        "ended_at": None,
        "lead_joined_at": None,
    }
    with patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value=row,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        return_value=(False, "https://db.example.com/message.wav?token=secret", "drop-1"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.request_manual_voicemail_drop",
        return_value={"accepted": True, "state": "waiting"},
    ) as request_drop, patch(
        "api.services.telephony.providers.twilio.routes.claim_manual_voicemail_playback",
    ) as claim:
        response = TestClient(_make_authenticated_test_app()).post(
            "/dialer/manual-voicemail-drop",
            json={"parent_call_sid": parent_sid, "action": "send"},
        )

    assert response.status_code == 200
    assert response.json() == {"state": "waiting"}
    request_drop.assert_awaited_once_with(parent_call_sid=parent_sid, rep_user_id=row["rep_user_id"])
    claim.assert_not_awaited()


def test_manual_voicemail_status_exposes_called_leg_readiness():
    parent_sid = _LISTEN_SID
    child_sid = "CA" + "1234567890abcdef" * 2
    row = {
        "rep_user_id": "11111111-1111-1111-1111-111111111111",
        "provider": "twilio",
        "child_call_sid": child_sid,
        "amd_result": None,
        "manual_voicemail_requested_at": None,
        "voicemail_drop_started_at": None,
        "auto_voicemail_sent": False,
    }
    with patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value=row,
    ):
        response = TestClient(_make_authenticated_test_app()).get(
            f"/dialer/manual-voicemail-drop/{parent_sid}"
        )

    assert response.status_code == 200
    assert response.json() == {"state": "idle", "amd_result": None, "ready": True, "stage": None, "stage_at": None, "stage_detail": {}}

    row["child_call_sid"] = None
    with patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value=row,
    ):
        response = TestClient(_make_authenticated_test_app()).get(
            f"/dialer/manual-voicemail-drop/{parent_sid}"
        )
    assert response.status_code == 200
    assert response.json()["ready"] is False


def test_manual_voicemail_redirects_only_after_beep_and_lead_join(monkeypatch):
    parent_sid = _LISTEN_SID
    child_sid = "CA" + "1234567890abcdef" * 2
    row = {
        "rep_user_id": "11111111-1111-1111-1111-111111111111",
        "provider": "twilio",
        "child_call_sid": child_sid,
        "amd_result": "machine_end_beep",
        "manual_voicemail_requested_at": None,
        "voicemail_drop_started_at": None,
        "auto_voicemail_sent": False,
        "ended_at": None,
        "lead_joined_at": "2026-10-15T00:00:00Z",
    }
    twilio = MagicMock()
    with patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value=row,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_auto_voicemail_settings",
        return_value=(False, "https://db.example.com/message.wav?token=secret", "drop-1"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.request_manual_voicemail_drop",
        return_value={"accepted": True, "state": "ready"},
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_manual_voicemail_playback",
        return_value=True,
    ) as claim, patch(
        "api.services.telephony.providers.twilio.routes._twilio_client",
        return_value=twilio,
    ), patch(
        "api.services.telephony.providers.twilio.routes.release_manual_voicemail_playback",
    ) as release, patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
        new=AsyncMock(return_value=True),
    ) as release_rep:
        response = TestClient(_make_authenticated_test_app()).post(
            "/dialer/manual-voicemail-drop",
            json={"parent_call_sid": parent_sid, "action": "send"},
        )

    assert response.status_code == 200
    # The prospect leg plays it; the rep is released straight away.
    assert response.json() == {"state": "playing", "released": True}
    release_rep.assert_awaited_once_with(call_sid=parent_sid)
    claim.assert_awaited_once_with(parent_call_sid=parent_sid, child_call_sid=child_sid)
    twilio.calls.assert_called_once_with(child_sid)
    twilio.calls.return_value.update.assert_called_once()
    twiml = twilio.calls.return_value.update.call_args.kwargs["twiml"]
    assert "<Play>https://db.example.com/message.wav?token=secret</Play>" in twiml
    assert "dialer-auto-voicemail-complete" in twiml
    release.assert_not_awaited()


def test_dialer_conference_join_never_autodrops_on_unknown_answer(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
    ) as get_call:
        response = client.post(
            f"/dialer-conference-join?conference_name=call-{parent_sid}&parent_call_sid={parent_sid}",
            data={"AnsweredBy": "unknown"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    get_call.assert_not_awaited()


def test_auto_voicemail_completion_records_sent_and_ends_rep_leg(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"child_call_sid": "CA" + "0123456789abcdef" * 2},
    ), patch(
        "api.services.telephony.providers.twilio.routes.mark_auto_voicemail_sent",
    ) as mark_sent, patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as end_rep:
        response = client.post(
            f"/dialer-auto-voicemail-complete?parent_call_sid={parent_sid}",
            data={"CallSid": "CA" + "0123456789abcdef" * 2},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    mark_sent.assert_awaited_once_with(parent_call_sid=parent_sid, child_call_sid=None)
    end_rep.assert_awaited_once_with(call_sid=parent_sid)


@pytest.mark.parametrize(
    ("sibling_status", "ends_rep"),
    [("ringing", False), ("completed", True)],
)
def test_parallel_voicemail_completion_ends_only_its_leg_until_batch_is_terminal(
    monkeypatch, sibling_status, ends_rep
):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    parent_sid = _LISTEN_SID
    child_sid = "CA" + "0123456789abcdef" * 2
    leg_id = "11111111-1111-4111-8111-111111111111"
    entry_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    sibling_id = "22222222-2222-4222-8222-222222222222"
    sibling_entry = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    before = [
        {"leg_id": leg_id, "entry_id": entry_id, "child_call_sid": child_sid, "status": "machine"},
        {"leg_id": sibling_id, "entry_id": sibling_entry, "child_call_sid": "CA" + "f" * 32, "status": "ringing"},
    ]
    after = [
        {**before[0], "status": "voicemail", "auto_voicemail_sent": True},
        {**before[1], "status": sibling_status},
    ]
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_legs",
        side_effect=[before, after],
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_session",
        return_value={"winning_child_call_sid": None},
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.update_parallel_leg",
    ) as update_leg, patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.finish_parallel_session",
    ) as finish_session, patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as update_call, patch(
        "api.services.telephony.providers.twilio.routes.mark_auto_voicemail_sent",
    ) as mark_sent, patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as cancel:
        response = client.post(
            f"/dialer-auto-voicemail-complete?parent_call_sid={parent_sid}&parallel_leg_id={leg_id}",
            data={"CallSid": child_sid},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    update_leg.assert_awaited_once_with(child_call_sid=child_sid, leg_id=leg_id, status="voicemail")
    mark_sent.assert_awaited_once_with(parent_call_sid=parent_sid, child_call_sid=child_sid)
    if ends_rep:
        finish_session.assert_awaited_once_with(parent_call_sid=parent_sid, status="failed")
        update_call.assert_awaited_once_with(
            parent_call_sid=parent_sid, child_call_sid=None, status="failed", duration_seconds=0
        )
        cancel.assert_awaited_once_with(call_sid=parent_sid)
    else:
        finish_session.assert_not_awaited()
        update_call.assert_not_awaited()
        cancel.assert_not_awaited()


def test_dialer_conference_join_repeats_conference_telemetry_attributes(monkeypatch):
    """Twilio honours conference-level attributes only from whoever CREATES
    the conference. The rep usually wins that race, but if the lead ever gets
    there first (instant-answer voicemail, SIP endpoint, slow rep leg) and
    only the rep's leg carried these, the call would come up unrecorded with
    no conference-start event - hence no conference_sid to correlate on
    either. Both legs must carry them."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111"
            "&events_url=https%3A%2F%2Fapi.example.com%2Fapi%2Fv1%2Ftelephony%2F"
            "dialer-conference-events"
            "&recording_url=https%3A%2F%2Fapi.example.com%2Fapi%2Fv1%2Ftelephony%2F"
            "dialer-recording-callback",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert 'record="record-from-start"' in response.text
    assert 'statusCallbackEvent="start end join leave"' in response.text
    assert (
        'statusCallback="https://api.example.com/api/v1/telephony/dialer-conference-events"'
        in response.text
    )
    assert (
        'recordingStatusCallback="https://api.example.com/api/v1/telephony/'
        'dialer-recording-callback"' in response.text
    )
    # The participant-level attributes must survive the addition.
    assert 'muted="false"' in response.text
    assert 'endConferenceOnExit="true"' in response.text
    assert "call-CA111</Conference>" in response.text


def test_dialer_conference_join_omits_telemetry_attributes_when_urls_absent(monkeypatch):
    """No URLs (an in-flight join URL issued by the previous deploy) degrades
    to the old attribute-free leg rather than emitting relative callback URLs
    Twilio would reject - a rejected TwiML costs the lead the whole leg."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "record=" not in response.text
    assert "statusCallback" not in response.text
    assert "call-CA111</Conference>" in response.text


def test_dialer_conference_join_ignores_relative_callback_urls(monkeypatch):
    """An unresolved backend endpoint yields "/api/v1/..." - relative, not
    empty - and Twilio rejects a relative callback URL, so the attributes are
    dropped rather than emitted broken."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111"
            "&events_url=%2Fapi%2Fv1%2Ftelephony%2Fdialer-conference-events"
            "&recording_url=%2Fapi%2Fv1%2Ftelephony%2Fdialer-recording-callback",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "record=" not in response.text
    assert "statusCallback" not in response.text


def test_dialer_conference_join_respects_muted_param(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111&muted=true",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert 'muted="true"' in response.text


def test_dialer_conference_join_hangs_up_on_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111",
            data={},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert "<Hangup/>" in response.text


def test_dialer_conference_join_hangs_up_when_conference_name_missing(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert "<Hangup/>" in response.text


def test_dialer_conference_join_respects_end_on_exit_param(monkeypatch):
    """The param still overrides the (now "true") default - a manager's
    listen-in leg will need end_on_exit=false so leaving doesn't kill the
    call it was monitoring."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111&end_on_exit=false",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert 'endConferenceOnExit="false"' in response.text


def test_dialer_conference_join_respects_start_on_enter_param(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111&start_on_enter=false",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert 'startConferenceOnEnter="false"' in response.text


def test_dialer_conference_join_escapes_conference_name(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    malicious_name = "call-CA111</Conference></Dial><Dial><Number>+19005551234</Number></Dial>"

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join",
            params={"conference_name": malicious_name},
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Number>+19005551234</Number>" not in response.text
    assert "&lt;/Conference&gt;" in response.text


def test_dialer_conference_events_updates_conference_sid_on_start(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-conference-events",
            data={
                "StatusCallbackEvent": "conference-start",
                "ConferenceSid": "CF999",
                "FriendlyName": "call-CA111",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_awaited_once_with(parent_call_sid="CA111", conference_sid="CF999")


@pytest.mark.parametrize(
    ("participant_sid", "winner_sid", "expected_kind"),
    [
        (_LISTEN_SID, "CA" + "1" * 32, "rep_join"),
        ("CA" + "1" * 32, "CA" + "1" * 32, "lead_join"),
    ],
)
def test_parallel_conference_join_records_provider_event_time(
    monkeypatch, participant_sid, winner_sid, expected_kind
):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_session",
        new_callable=AsyncMock,
        return_value={"winning_child_call_sid": winner_sid},
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.record_parallel_timing",
        new_callable=AsyncMock,
    ) as record_timing, patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = client.post(
            "/dialer-conference-events",
            data={
                "StatusCallbackEvent": "participant-join",
                "FriendlyName": f"call-{_LISTEN_SID}",
                "CallSid": participant_sid,
                "Timestamp": "Mon, 18 Oct 2026 12:34:57 +0000",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    record_timing.assert_awaited_once_with(
        parent_call_sid=_LISTEN_SID,
        child_call_sid=participant_sid,
        event_kind=expected_kind,
        event_at="2026-10-18T12:34:57+00:00",
    )


@pytest.mark.parametrize("stored", [True, False, None])
def test_teammate_conference_records_actual_overflow_join(monkeypatch, stored):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.dialer_parallel.parallel_session",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.telephony.providers.twilio.routes.record_overflow_join",
        new=AsyncMock(return_value=stored),
    ) as record, patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        new=AsyncMock(return_value=None),
    ):
        response = client.post("/dialer-conference-events", data={
            "StatusCallbackEvent": "participant-join", "FriendlyName": f"call-{_LISTEN_SID}",
            "CallSid": "CA" + "1" * 32, "Timestamp": "Mon, 18 Oct 2026 12:34:57 +0000",
        }, headers={"X-Twilio-Signature": "fake-signature"})
    assert response.status_code == (503 if stored is None else 200)
    record.assert_awaited_once_with(
        conference_name=f"call-{_LISTEN_SID}", provider="twilio",
        participant_call_id="CA" + "1" * 32, event_at="2026-10-18T12:34:57+00:00",
    )


def test_dialer_conference_events_does_not_write_conference_sid_on_end(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_conference_sid",
    ) as mock_update, patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ):
        response = client.post(
            "/dialer-conference-events",
            data={
                "StatusCallbackEvent": "conference-end",
                "ConferenceSid": "CF999",
                "FriendlyName": "call-CA111",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_not_awaited()


def test_dialer_conference_events_ignores_unrecognized_friendly_name(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-conference-events",
            data={
                "StatusCallbackEvent": "conference-start",
                "ConferenceSid": "CF999",
                "FriendlyName": "some-other-conference",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_not_awaited()


def test_dialer_conference_events_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_conference_sid",
    ) as mock_update:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-start"},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert response.status_code == 401
    mock_update.assert_not_awaited()


def test_dialer_conference_events_cancels_orphaned_ringing_lead_leg(monkeypatch):
    """The rep hanging up while the lead is still ringing ends the conference,
    but the lead's leg is not a participant yet - without an explicit cancel
    it keeps ringing and becomes an abandoned call from our own caller ID."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"child_call_sid": "CA222", "status": "ringing"},
    ) as mock_lookup, patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={
                "StatusCallbackEvent": "conference-end",
                "ConferenceSid": "CF999",
                "FriendlyName": "call-CA111",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_lookup.assert_awaited_once_with(parent_call_sid="CA111")
    mock_cancel.assert_awaited_once_with(call_sid="CA222")


@pytest.mark.parametrize(
    "status,should_cancel",
    [
        # Pre-answer: the leg is still in flight and was never a conference
        # participant, so nothing else will ever end it.
        ("queued", True),
        ("initiated", True),
        ("ringing", True),
        # Unknown/missing status fails TOWARD cancelling - the polarity that
        # matters when the thing being prevented is an abandoned call.
        ("", True),
        (None, True),
        # Answered: a real participant, ended by Twilio's own conference
        # teardown.
        ("in-progress", False),
        ("answered", False),
        ("completed", False),
        # Terminal-but-unanswered. Ring-out (no-answer) is the routine one on
        # a power dialer: the lead's leg times out while the rep is still in
        # the conference, and only then does the rep hang up. Cancelling here
        # would be rejected by Twilio and log an error on a normal call -
        # burying the one signal that says orphan protection actually failed.
        ("busy", False),
        ("no-answer", False),
        ("canceled", False),
        ("failed", False),
    ],
)
def test_dialer_conference_events_cancels_only_unsettled_lead_legs(
    monkeypatch, status, should_cancel
):
    """Pinned against Twilio's real CallStatus vocabulary, not against the
    constant itself - a test that only loops over values already in the set
    asserts nothing."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"child_call_sid": "CA222", "status": status},
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    if should_cancel:
        mock_cancel.assert_awaited_once_with(call_sid="CA222")
    else:
        mock_cancel.assert_not_awaited()


def test_dialer_conference_events_handles_missing_child_call_sid(monkeypatch):
    """child_call_sid is written by the lead's own status callback, so a
    conference that ends immediately can race it. Nothing to cancel by SID -
    the webhook must still succeed rather than make Twilio retry."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    for leg in (None, {"child_call_sid": None, "status": "initiated"}):
        with patch(
            "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
            return_value=True,
        ), patch(
            "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
            return_value=leg,
        ), patch(
            "api.services.telephony.providers.twilio.routes.cancel_call",
        ) as mock_cancel:
            response = client.post(
                "/dialer-conference-events",
                data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
                headers={"X-Twilio-Signature": "fake-signature"},
            )

        assert response.status_code == 200
        mock_cancel.assert_not_awaited()


def test_dialer_conference_events_ignores_conference_end_for_unrecognized_name(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
    ) as mock_lookup, patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "some-other-conference"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_lookup.assert_not_awaited()
    mock_cancel.assert_not_awaited()


def test_dialer_conference_events_ignores_participant_events(monkeypatch):
    """statusCallbackEvent is "start end join leave", so participant events
    arrive here too and must not touch the lead's leg."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_conference_sid",
    ) as mock_update, patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
    ) as mock_lookup, patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={
                "StatusCallbackEvent": "participant-leave",
                "ConferenceSid": "CF999",
                "FriendlyName": "call-CA111",
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_not_awaited()
    mock_lookup.assert_not_awaited()
    mock_cancel.assert_not_awaited()


def test_voice_connect_marks_dialer_call_failed_when_lead_dial_fails(monkeypatch):
    """Nothing else ever moves this row on: the only caller of
    update_dialer_call_status is the dialer-call-status webhook, whose URL
    rides on the lead's leg - which was never created."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as mock_update:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    mock_update.assert_awaited_once_with(
        parent_call_sid="CA111",
        child_call_sid=None,
        status="failed",
        duration_seconds=0,
    )


def test_voice_connect_does_not_mark_failed_on_success(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ) as mock_update:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    mock_update.assert_not_awaited()


def test_voice_connect_persists_child_call_sid_immediately(monkeypatch):
    """The orphan cleanup on conference-end needs the lead leg's SID to end
    it. Waiting for the lead's own status callback to write it leaves a race
    a fast rep hang-up can win, so voice-connect persists it on the spot."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_child_sid",
    ) as mock_child_sid:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    mock_child_sid.assert_awaited_once_with(parent_call_sid="CA111", child_call_sid="CA222")


def test_voice_connect_does_not_persist_child_call_sid_when_dial_fails(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")

    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_child_sid",
    ) as mock_child_sid:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    mock_child_sid.assert_not_awaited()


@contextmanager
def _captured_error_logs():
    """loguru doesn't feed pytest's caplog, so grab a sink directly."""
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def test_dialer_conference_events_stays_quiet_when_no_dialer_calls_row(monkeypatch):
    """No row exists for an unrecognized rep identity, so conference-end for
    one is routine, not an anomaly - it must not log at ERROR. That noise is
    the same category the status gate exists to eliminate."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel, _captured_error_logs() as errors:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_cancel.assert_not_awaited()
    assert errors == []


def test_dialer_conference_events_errors_when_row_exists_without_child_sid(monkeypatch):
    """The other side of that coin: a row that EXISTS but never got its lead
    SID means the write failed and the leg is unreachable - a real anomaly,
    and this is the only signal for it."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"child_call_sid": None, "status": "ringing"},
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel, _captured_error_logs() as errors:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_cancel.assert_not_awaited()
    assert len(errors) == 1
    assert "child_call_sid" in errors[0]


def test_dialer_conference_events_survives_a_raising_lookup(monkeypatch):
    """The cleanup helper must be fail-soft on its own terms, not merely by
    delegation: an exception in its own code (or from a callee that stops
    swallowing its errors) would otherwise 500 and earn a Twilio retry."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        side_effect=RuntimeError("supabase exploded"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_cancel.assert_not_awaited()


def test_dialer_conference_events_survives_a_malformed_lookup_result(monkeypatch):
    """Belt and braces for the bug this pairs with: even if
    get_dialer_call_child_leg ever hands back a non-dict, the webhook must
    not 500."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value="not-a-dict",
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_cancel.assert_not_awaited()


def test_dialer_listen_connect_allows_manager(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert 'muted="true"' in response.text
    assert f"call-{_LISTEN_SID}</Conference>" in response.text
    # beep="false" is the whole point of a SILENT monitor: Twilio defaults to
    # beep="true", which would play a join tone to the rep AND the lead.
    assert 'beep="false"' in response.text
    # The manager leaving must not end the call; joining an already-ended
    # conference must not resurrect it.
    assert 'endConferenceOnExit="false"' in response.text
    assert 'startConferenceOnEnter="false"' in response.text
    # The disclosure lives on the lead's leg only - it must never be played
    # into a live call by someone joining to listen.
    assert _DISCLOSURE not in response.text
    mock_create_listener.assert_awaited_once_with(
        parent_call_sid=_LISTEN_SID,
        manager_user_id="00000000-0000-0000-0000-000000000001",
        manager_call_sid=_MANAGER_LISTEN_SID,
        session_id=None,
        mode="listen",
    )
    # Well-formedness is asserted, not eyeballed: parse the document rather
    # than trusting substring matches against an f-string.
    doc = xml.dom.minidom.parseString(response.text)
    assert len(doc.getElementsByTagName("Conference")) == 1


@pytest.mark.parametrize(
    ("mode", "expected_attrs"),
    [
        ("whisper", ['muted="false"', f'coach="{_LISTEN_SID}"']),
        ("barge", ['muted="false"']),
    ],
)
def test_dialer_listen_connect_uses_requested_supervisor_mode(
    monkeypatch, mode, expected_attrs
):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={
                "From": "client:rep-7",
                "ListenParentCallSid": _LISTEN_SID,
                "MonitorMode": mode,
                "CallSid": _MANAGER_LISTEN_SID,
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert all(attr in response.text for attr in expected_attrs)
    assert 'beep="false"' in response.text
    assert 'endConferenceOnExit="false"' in response.text
    mock_create_listener.assert_awaited_once_with(
        parent_call_sid=_LISTEN_SID,
        manager_user_id="00000000-0000-0000-0000-000000000001",
        manager_call_sid=_MANAGER_LISTEN_SID,
        session_id=None,
        mode=mode,
    )


def test_dialer_listen_connect_rejects_non_manager(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000002")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    # An unauthorized caller must leave no trace of having listened.
    mock_create_listener.assert_not_awaited()


def test_dialer_listen_connect_hangs_up_on_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()


def test_dialer_listen_connect_hangs_up_when_parent_call_sid_missing(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()


def test_dialer_listen_connect_hangs_up_for_unrecognized_identity(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:not-a-rep", "ListenParentCallSid": _LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()


def test_dialer_listen_connect_hangs_up_when_user_has_no_provider_id(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=MagicMock(provider_id=None),
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()


def test_dialer_listen_connect_rejects_malformed_parent_call_sid(monkeypatch):
    """The SID becomes a conference name, so it is constrained to Twilio's own
    grammar ("CA" + 32 hex) before it gets there. Without this an
    authenticated manager could name an arbitrary conference and write a
    dialer_call_listeners row for a call that never existed - there is no FK
    on that column."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=MagicMock(provider_id="00000000-0000-0000-0000-000000000001"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": "not-a-sid"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()


def test_dialer_listen_connect_survives_a_raising_user_lookup(monkeypatch):
    """Pins the blanket try/except: db_client.get_user_by_id is the one call
    on this path that does NOT swallow its own errors, and it sits between two
    that do. An escape becomes a 500, which Twilio reads aloud to the manager
    and then retries. Deleting the except must fail this test."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        side_effect=RuntimeError("database is on fire"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()


def test_dialer_conference_join_twiml_is_well_formed_under_injection(monkeypatch):
    """Parses the document instead of substring-matching it.

    Aimed at dialer-conference-join rather than dialer-listen-connect on
    purpose: listen-in now validates its SID shape, so the conference name
    there can no longer be arbitrary and its escape() is belt-and-braces.
    THIS endpoint still takes a free-form conference_name, so escaping is the
    only defense here - which makes this the layer worth pinning. Deleting
    escape() must fail this test."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())
    malicious_name = "call-CA111</Conference></Dial><Dial><Number>+19005551234</Number></Dial>"

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join",
            params={"conference_name": malicious_name},
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    doc = xml.dom.minidom.parseString(response.text)
    conferences = doc.getElementsByTagName("Conference")
    assert len(conferences) == 1
    assert len(doc.getElementsByTagName("Number")) == 0
    # The payload survives intact as TEXT - it is the conference name, escaped,
    # not markup.
    assert conferences[0].firstChild.data == malicious_name


def test_voice_connect_delegates_to_listen_in_for_manager(monkeypatch):
    """The Voice SDK cannot choose a URL - one TwiML App, one Voice URL, and
    it points here - so a manager's listen-in leg arrives at voice-connect
    with a ListenParentCallSid and no To. Without this delegation the endpoint
    is unreachable from a browser."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as mock_dial, patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ) as mock_create_call:
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert 'muted="true"' in response.text
    assert 'beep="false"' in response.text
    assert 'endConferenceOnExit="false"' in response.text
    assert 'startConferenceOnEnter="false"' in response.text
    assert f"call-{_LISTEN_SID}</Conference>" in response.text
    mock_create_listener.assert_awaited_once_with(
        parent_call_sid=_LISTEN_SID,
        manager_user_id="00000000-0000-0000-0000-000000000001",
        manager_call_sid=_MANAGER_LISTEN_SID,
        session_id=None,
        mode="listen",
    )
    # Listening must never place an outbound call or log a dial.
    mock_dial.assert_not_awaited()
    mock_create_call.assert_not_awaited()


def test_voice_connect_delegation_still_rejects_a_non_manager(monkeypatch):
    """Delegation is routing, not an authorization bypass: a sales_rep who
    hand-crafts a ListenParentCallSid onto their own dial gets nothing."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000002")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener, patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as mock_dial:
        response = client.post(
            "/voice-connect",
            data={
                "To": "+15559876543",
                "From": "client:rep-7",
                "CallSid": "CA111",
                "ListenParentCallSid": _LISTEN_SID,
            },
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()
    # Nor may the rejected listen attempt fall through into a normal dial -
    # the To/CallSid here would otherwise be a perfectly valid outbound call.
    mock_dial.assert_not_awaited()


def test_voice_connect_rep_dial_is_untouched_without_listen_param(monkeypatch):
    """The hot path on every sales call. Delegation must be invisible to it."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("SYSEVO_TWILIO_DEFAULT_CALLER_ID", "+15551234567")
    client = TestClient(_make_test_app())
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000003")

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=fake_user,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ) as mock_dial, patch(
        "api.services.telephony.providers.twilio.routes.create_dialer_call",
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_child_sid",
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_manager_or_admin",
    ) as mock_is_manager, patch(
        "api.services.telephony.providers.twilio.routes.claim_floor_monitor",
        return_value={"allowed": True, "conference_name": f"call-{_LISTEN_SID}", "coach_call_sid": _LISTEN_SID},
    ) as mock_create_listener:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "From": "client:rep-7", "CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    # A normal rep dial: unmuted, it starts and ends the conference.
    assert 'startConferenceOnEnter="true"' in response.text
    assert 'endConferenceOnExit="true"' in response.text
    assert "call-CA111</Conference>" in response.text
    mock_dial.assert_awaited_once()
    # Not so much as a role lookup on the dial path.
    mock_is_manager.assert_not_awaited()
    mock_create_listener.assert_not_awaited()


def test_floor_whisper_uses_server_inbound_conference_and_rep_leg(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    rep_sid = "CA" + "3" * 32
    request_id = "20000000-0000-0000-0000-000000000001"
    with patch("api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True), \
         patch("api.services.telephony.providers.twilio.routes.db_client.get_user_by_id", return_value=MagicMock(provider_id="10000000-0000-0000-0000-000000000011")), \
         patch("api.services.telephony.providers.twilio.routes.is_manager_or_admin", return_value=True), \
         patch("api.services.telephony.providers.twilio.routes.claim_floor_monitor", return_value={"allowed": True, "conference_name": "inbound-real-conference", "coach_call_sid": rep_sid}) as claim:
        response = TestClient(_make_test_app()).post("/voice-connect", data={
            "From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID,
            "CallSid": _MANAGER_LISTEN_SID, "MonitorMode": "whisper", "MonitorSessionId": request_id,
        }, headers={"X-Twilio-Signature": "test"})
    assert response.status_code == 200
    assert f'coach="{rep_sid}"' in response.text
    assert f'coach="{_LISTEN_SID}"' not in response.text
    assert "inbound-real-conference</Conference>" in response.text
    assert claim.await_args.kwargs["session_id"] == request_id
    assert claim.await_args.kwargs["manager_call_sid"] == _MANAGER_LISTEN_SID


def test_floor_denied_or_unavailable_state_never_returns_conference_twiml(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    with patch("api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True), \
         patch("api.services.telephony.providers.twilio.routes.db_client.get_user_by_id", return_value=MagicMock(provider_id="10000000-0000-0000-0000-000000000011")), \
         patch("api.services.telephony.providers.twilio.routes.is_manager_or_admin", return_value=True), \
         patch("api.services.telephony.providers.twilio.routes.claim_floor_monitor", return_value=None):
        response = TestClient(_make_test_app()).post("/voice-connect", data={
            "From": "client:rep-7", "ListenParentCallSid": _LISTEN_SID, "CallSid": _MANAGER_LISTEN_SID,
        }, headers={"X-Twilio-Signature": "test"})
    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text


@pytest.mark.parametrize(("closed", "status"), [(True, 200), (False, 200), (None, 503)])
def test_floor_signed_participant_leave_closes_its_sdk_leg_and_retries_on_outage(monkeypatch, closed, status):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    with patch("api.services.telephony.providers.twilio.routes.RequestValidator.validate", return_value=True), \
         patch("api.services.telephony.providers.twilio.routes.close_floor_monitor_for_provider", return_value=closed) as close:
        response = TestClient(_make_test_app()).post("/dialer-conference-events", data={
            "StatusCallbackEvent": "participant-leave", "CallSid": _MANAGER_LISTEN_SID,
            "FriendlyName": "inbound-real-conference",
        }, headers={"X-Twilio-Signature": "test"})
    assert response.status_code == status
    close.assert_awaited_once_with(_MANAGER_LISTEN_SID)


def _post_outbound_voice_connect(call_sid):
    client = TestClient(_make_test_app())
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_caller_id",
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        return_value=_DIALER_REP,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ):
        return client.post(
            "/voice-connect",
            data={"From": "client:rep-7", "To": "+15559876543", "CallSid": call_sid},
            headers={"X-Twilio-Signature": "fake-signature"},
        )


def test_voice_connect_rep_leg_streams_both_tracks_for_live_captions(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.delenv("SYSEVO_DIALER_LIVE_TRANSCRIBE", raising=False)
    sid = "CA" + "a" * 32
    response = _post_outbound_voice_connect(sid)
    assert response.status_code == 200
    text = response.text
    assert "<Start><Stream " in text
    assert text.index("<Start>") < text.index("<Dial>")
    assert 'url="wss://api.example.com/api/v1/telephony/dialer-twilio-transcribe"' in text
    assert 'track="both_tracks"' in text
    assert f'<Parameter name="channel_key" value="{sid}"/>' in text
    assert f"call-{sid}</Conference>" in text
    assert _DISCLOSURE not in text


def test_voice_connect_rep_leg_has_no_stream_without_deepgram(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    sid = "CA" + "a" * 32
    response = _post_outbound_voice_connect(sid)
    assert response.status_code == 200
    assert "<Start>" not in response.text
    assert f"call-{sid}</Conference>" in response.text


def test_inbound_rep_join_streams_captions_keyed_by_the_callers_call(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.delenv("SYSEVO_DIALER_LIVE_TRANSCRIBE", raising=False)
    parent_sid = "CA" + "abcdef0123456789" * 2
    conference_name = f"call-{parent_sid}"
    client = TestClient(_make_test_app())
    rep = SimpleNamespace(provider_id="11111111-1111-1111-1111-111111111111")
    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.db_client.get_user_by_id",
        new=AsyncMock(return_value=rep),
    ), patch(
        "api.services.telephony.providers.twilio.routes.is_claimed_inbound_conference",
        new=AsyncMock(return_value=True),
    ), patch(
        "api.services.telephony.providers.twilio.routes.bind_inbound_rep_leg",
        new=AsyncMock(return_value=True),
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        new=AsyncMock(return_value=("https://api.example.com", "wss://api.example.com")),
    ):
        response = client.post(
            "/voice-connect",
            data={"From": "client:rep-123", "To": conference_name, "conference": conference_name,
                  "CallSid": "CA" + "1" * 32},
            headers={"X-Twilio-Signature": "fake-signature"},
        )
    assert response.status_code == 200
    text = response.text
    assert f'<Parameter name="channel_key" value="{parent_sid}"/>' in text
    assert text.index("<Start>") < text.index("<Dial>")
    assert _DISCLOSURE in text
