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
        "api.services.telephony.providers.twilio.routes.get_dialer_provider",
        return_value=provider,
    ):
        result = await get_voice_token(user=fake_user)

    assert result.provider == "twilio"
    assert result.destination == ""
