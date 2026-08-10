import pytest

from api.services.telephony.providers.twilio.voice_sdk import (
    VoiceSdkNotConfigured,
    generate_voice_access_token,
)


def test_generate_voice_access_token_returns_three_part_jwt(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "AC" + "x" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SK" + "x" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("TWILIO_TWIML_APP_SID", "AP" + "x" * 32)

    jwt_str = generate_voice_access_token("rep-42")

    assert isinstance(jwt_str, str)
    assert len(jwt_str.split(".")) == 3


def test_generate_voice_access_token_raises_when_not_configured(monkeypatch):
    for var in (
        "SYSEVO_TWILIO_ACCOUNT_SID",
        "TWILIO_API_KEY_SID",
        "TWILIO_API_KEY_SECRET",
        "TWILIO_TWIML_APP_SID",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(VoiceSdkNotConfigured):
        generate_voice_access_token("rep-42")
