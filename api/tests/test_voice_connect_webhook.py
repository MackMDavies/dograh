from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from api.services.telephony.providers.twilio.routes import router

_DISCLOSURE = "<Say>This call may be recorded and monitored for quality assurance.</Say>"


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


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
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
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
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as mock_dial:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
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
            "dialer-recording-callback"
        ),
        status_callback_url=(
            "https://api.example.com/api/v1/telephony/dialer-call-status?parent_call_sid=CA111"
        ),
    )


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
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
    ) as mock_dial:
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
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
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        return_value=("https://api.example.com", "wss://api.example.com"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value=None,
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
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
    )


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
        return_value=None,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_backend_endpoints",
        side_effect=Exception("backend unreachable"),
    ), patch(
        "api.services.telephony.providers.twilio.routes.dial_lead_into_conference",
        return_value="CA222",
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "CallSid": "CA111"},
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
        return_value=None,
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
            data={"To": "+15559876543", "CallSid": "CA111"},
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
        return_value=None,
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
            data={"To": "+15559876543", "CallSid": "CA111"},
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
        return_value=None,
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
            data={"To": "+15559876543", "CallSid": "CA111"},
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
        "api.services.telephony.providers.twilio.routes.create_dialer_call_listener",
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Conference" in response.text
    assert 'muted="true"' in response.text
    assert "call-CA111</Conference>" in response.text
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
        parent_call_sid="CA111",
        manager_user_id="00000000-0000-0000-0000-000000000001",
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
        "api.services.telephony.providers.twilio.routes.create_dialer_call_listener",
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": "CA111"},
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
        "api.services.telephony.providers.twilio.routes.create_dialer_call_listener",
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": "CA111"},
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
        "api.services.telephony.providers.twilio.routes.create_dialer_call_listener",
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
        "api.services.telephony.providers.twilio.routes.create_dialer_call_listener",
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:not-a-rep", "ListenParentCallSid": "CA111"},
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
        "api.services.telephony.providers.twilio.routes.create_dialer_call_listener",
    ) as mock_create_listener:
        response = client.post(
            "/dialer-listen-connect",
            data={"From": "client:rep-7", "ListenParentCallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
    assert "<Conference" not in response.text
    mock_create_listener.assert_not_awaited()
