from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.telephony.providers.twilio.routes import router


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_voice_connect_returns_dial_twiml_for_valid_signature(monkeypatch):
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
    assert '<Dial callerId="+15551234567">' in response.text
    assert "<Number>+15559876543</Number>" in response.text


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
            data={"To": "+15559876543"},
            headers={"X-Twilio-Signature": "bad-signature"},
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
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "From": "client:rep-42"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert '<Dial callerId="+15559998888">' in response.text


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
    ):
        response = client.post(
            "/voice-connect",
            data={"To": "+15559876543", "From": "client:rep-99"},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert '<Dial callerId="+15551234567">' in response.text


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
            data={},
            headers={"X-Twilio-Signature": "fake-signature"},
        )

    assert response.status_code == 200
    assert "<Hangup/>" in response.text
