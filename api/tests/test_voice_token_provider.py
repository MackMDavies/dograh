"""The /voice-token route must tell the browser which provider to use."""
from unittest.mock import AsyncMock, patch

from api.services.telephony.dialer.provider import DialerCredentials


async def test_voice_token_reports_active_provider():
    from api.services.telephony.providers.twilio.routes import get_voice_token

    fake_user = type("U", (), {"id": 42})()
    creds = DialerCredentials(
        token="sw-tok", identity="rep-42", destination="/private/dialer"
    )
    provider = AsyncMock()
    provider.name = "signalwire"
    provider.mint_credentials = AsyncMock(return_value=creds)

    with patch(
        "api.services.telephony.providers.twilio.routes.resolve_active_dialer_provider",
        return_value="signalwire",
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_dialer_number",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_provider",
        return_value=provider,
    ):
        result = await get_voice_token(user=fake_user)

    assert result.provider == "signalwire"
    assert result.token == "sw-tok"
    assert result.identity == "rep-42"
    assert result.destination == "/private/dialer"


async def test_voice_token_defaults_to_twilio():
    from api.services.telephony.providers.twilio.routes import get_voice_token

    fake_user = type("U", (), {"id": 7})()
    creds = DialerCredentials(token="tw-tok", identity="rep-7", destination="")
    provider = AsyncMock()
    provider.name = "twilio"
    provider.mint_credentials = AsyncMock(return_value=creds)

    with patch(
        "api.services.telephony.providers.twilio.routes.resolve_active_dialer_provider",
        return_value="twilio",
    ), patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_dialer_number",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_provider",
        return_value=provider,
    ):
        result = await get_voice_token(user=fake_user)

    assert result.provider == "twilio"
    assert result.destination == ""


async def test_voice_token_uses_telnyx_for_a_rep_assigned_a_telnyx_number():
    from api.services.telephony.providers.twilio.routes import get_voice_token

    fake_user = type("U", (), {"id": 42})()
    creds = DialerCredentials(
        token="telnyx-jwt", identity="rep-42", destination=""
    )
    boundary = "2026-10-05T00:00:00+00:00"
    provider = AsyncMock()
    provider.name = "telnyx"
    provider.mint_credentials = AsyncMock(return_value=creds)

    with patch(
        "api.services.telephony.providers.twilio.routes.resolve_assigned_dialer_number",
        new=AsyncMock(return_value={"provider": "telnyx", "phone_number": "+15551234567", "next_rotation_at": boundary}),
    ), patch(
        "api.services.telephony.providers.twilio.routes.get_dialer_provider",
        return_value=provider,
    ):
        result = await get_voice_token(user=fake_user)

    assert result.provider == "telnyx"
    assert result.token == "telnyx-jwt"
    assert result.caller_number == "+15551234567"
    assert result.next_rotation_at.isoformat() == boundary
