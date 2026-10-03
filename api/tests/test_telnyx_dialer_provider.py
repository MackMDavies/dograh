from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest

from api.services.telephony.dialer.telnyx_dialer import (
    TelnyxDialerProvider,
    TelnyxNotConfigured,
)


@pytest.mark.asyncio
async def test_mints_short_lived_token_without_exposing_api_key():
    response = Mock(text='"short-lived-token"')
    response.raise_for_status = Mock()
    http = AsyncMock()
    http.post = AsyncMock(return_value=response)
    context = AsyncMock()
    context.__aenter__.return_value = http

    with patch(
        "api.services.telephony.dialer.telnyx_dialer.db_client.get_platform_telnyx_dialer_credentials",
        new=AsyncMock(return_value={
            "api_key": "permanent-secret",
            "connection_id": "connection-id",
            "telephony_credential_id": "credential-id",
        }),
    ), patch(
        "api.services.telephony.dialer.telnyx_dialer.httpx.AsyncClient",
        return_value=context,
    ):
        credentials = await TelnyxDialerProvider().mint_credentials(user_id=42)

    assert credentials.token == "short-lived-token"
    assert credentials.identity == "rep-42"
    assert "permanent-secret" not in credentials.token
    http.post.assert_awaited_once_with(
        "https://api.telnyx.com/v2/telephony_credentials/credential-id/token",
        headers={"Authorization": "Bearer permanent-secret", "Accept": "text/plain"},
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_missing_telnyx_credentials_fails_closed():
    with patch(
        "api.services.telephony.dialer.telnyx_dialer.db_client.get_platform_telnyx_dialer_credentials",
        new=AsyncMock(return_value=None),
    ), patch.dict("os.environ", {"TELNYX_API_KEY": "", "TELNYX_DIALER_CREDENTIAL_ID": ""}):
        with pytest.raises(TelnyxNotConfigured, match="not configured"):
            await TelnyxDialerProvider().mint_credentials(user_id=42)


@pytest.mark.asyncio
async def test_answered_call_state_maps_to_supported_history_status():
    from api.services.telephony.providers.twilio.routes import (
        TelnyxDialerCallStatusRequest,
        update_telnyx_dialer_call_status,
    )

    user = type("U", (), {"provider_id": "00000000-0000-0000-0000-000000000042"})()
    body = TelnyxDialerCallStatusRequest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"), status="answered"
    )

    with patch(
        "api.services.telephony.providers.twilio.routes.update_dialer_call_status",
        new=AsyncMock(),
    ) as update:
        await update_telnyx_dialer_call_status(body=body, user=user)

    update.assert_awaited_once_with(
        parent_call_sid=str(body.call_id),
        child_call_sid=None,
        status="in-progress",
        duration_seconds=None,
        rep_user_id=user.provider_id,
        provider="telnyx",
    )
