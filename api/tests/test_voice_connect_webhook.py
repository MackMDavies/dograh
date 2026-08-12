from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"CallSid": "CA111", "RecordingSid": "RE999", "RecordingStatus": "completed"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_awaited_once_with(parent_call_sid="CA111", recording_sid="RE999")


def test_dialer_recording_callback_ignores_non_completed_status(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"CallSid": "CA111", "RecordingSid": "RE999", "RecordingStatus": "in-progress"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_update.assert_not_awaited()


def test_dialer_recording_callback_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=False,
    ), patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"CallSid": "CA111", "RecordingSid": "RE999"},
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
        "api.services.telephony.providers.twilio.routes.update_dialer_call_recording",
    ) as mock_update:
        response = client.post(
            "/dialer-recording-callback",
            data={"CallSid": "CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
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
    assert 'endConferenceOnExit="false"' in response.text
    assert 'startConferenceOnEnter="true"' in response.text
    assert 'beep="false"' in response.text
    # This is the lead's leg, so this is where the called party hears the
    # recording/monitoring disclosure.
    assert _DISCLOSURE in response.text
    assert response.text.index(_DISCLOSURE) < response.text.index("<Dial>")


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
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ):
        response = client.post(
            "/dialer-conference-join?conference_name=call-CA111&end_on_exit=true",
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert 'endConferenceOnExit="true"' in response.text


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
