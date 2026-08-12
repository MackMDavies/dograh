"""Unit tests for dialer_call_listeners.py - Supabase writes for who's listening on a live call."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from api.services.telephony.providers.twilio.dialer_call_listeners import (
    create_dialer_call_listener,
)


async def test_create_dialer_call_listener_posts_expected_payload(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_listeners.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_listeners.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_listeners.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await create_dialer_call_listener(
            parent_call_sid="CA111", manager_user_id="00000000-0000-0000-0000-000000000001"
        )

    fake_client.post.assert_awaited_once()
    call = fake_client.post.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_call_listeners"
    assert call.kwargs["json"] == {
        "parent_call_sid": "CA111",
        "manager_user_id": "00000000-0000-0000-0000-000000000001",
    }


async def test_create_dialer_call_listener_swallows_errors(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_listeners.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_listeners.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_listeners.httpx.AsyncClient",
        return_value=fake_client,
    ):
        # Must not raise - this runs inside a webhook that must always
        # return valid TwiML regardless of whether this write succeeds.
        await create_dialer_call_listener(
            parent_call_sid="CA111", manager_user_id="00000000-0000-0000-0000-000000000001"
        )
