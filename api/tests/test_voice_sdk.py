from unittest.mock import AsyncMock

import pytest

from api.db import db_client
from api.services.telephony.providers.twilio.voice_sdk import (
    VoiceSdkNotConfigured,
    generate_voice_access_token,
)


async def test_generate_voice_access_token_returns_three_part_jwt(monkeypatch):
    monkeypatch.setattr(
        db_client,
        "get_platform_dialer_credentials",
        AsyncMock(
            return_value={
                "account_sid": "AC" + "x" * 32,
                "api_key_sid": "SK" + "x" * 32,
                "api_key_secret": "test-secret",
                "twiml_app_sid": "AP" + "x" * 32,
            }
        ),
    )

    jwt_str = await generate_voice_access_token("rep-42")

    assert isinstance(jwt_str, str)
    assert len(jwt_str.split(".")) == 3


async def test_generate_voice_access_token_raises_when_not_configured(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "AC" + "x" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SK" + "x" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("TWILIO_TWIML_APP_SID", "AP" + "x" * 32)
    monkeypatch.setattr(
        db_client,
        "get_platform_dialer_credentials",
        AsyncMock(return_value=None),
    )

    with pytest.raises(VoiceSdkNotConfigured):
        await generate_voice_access_token("rep-42")
