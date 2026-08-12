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


def test_dialer_conference_events_ignores_non_start_events(monkeypatch):
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


def test_dialer_conference_events_cancels_lead_leg_with_unknown_status(monkeypatch):
    """A row whose status never got written (or is null) is treated as
    "never answered" - cancelling is a no-op on Twilio's side if it did."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    with patch(
        "api.services.telephony.providers.twilio.routes.RequestValidator.validate",
        return_value=True,
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_call_child_leg",
        return_value={"child_call_sid": "CA222", "status": None},
    ), patch(
        "api.services.telephony.providers.twilio.routes.cancel_call",
    ) as mock_cancel:
        response = client.post(
            "/dialer-conference-events",
            data={"StatusCallbackEvent": "conference-end", "FriendlyName": "call-CA111"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    mock_cancel.assert_awaited_once_with(call_sid="CA222")


def test_dialer_conference_events_does_not_cancel_answered_lead_leg(monkeypatch):
    """A lead who answered IS a conference participant, so Twilio's own
    teardown ends their leg - issuing a cancel would just be a rejected
    Twilio call and an error log on every normal call."""
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    client = TestClient(_make_test_app())

    for status in ("in-progress", "completed", "answered"):
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

        assert response.status_code == 200, status
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
