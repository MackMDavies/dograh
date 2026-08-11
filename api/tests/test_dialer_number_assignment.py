"""Unit tests for dialer_number_assignment.py - per-rep caller ID resolution."""
from unittest.mock import AsyncMock, MagicMock, patch

from api.services.telephony.providers.twilio.dialer_number_assignment import (
    _parse_rep_id_from_identity,
    resolve_assigned_caller_id,
)


def test_parse_rep_id_strips_client_prefix():
    assert _parse_rep_id_from_identity("client:rep-42") == 42


def test_parse_rep_id_without_client_prefix():
    assert _parse_rep_id_from_identity("rep-7") == 7


def test_parse_rep_id_returns_none_for_non_rep_identity():
    assert _parse_rep_id_from_identity("client:something-else") is None


def test_parse_rep_id_returns_none_for_empty_string():
    assert _parse_rep_id_from_identity("") is None


async def test_resolve_assigned_caller_id_returns_none_for_unrecognized_identity():
    result = await resolve_assigned_caller_id("client:not-a-rep")
    assert result is None


async def test_resolve_assigned_caller_id_returns_none_when_user_has_no_provider_id():
    fake_user = MagicMock(provider_id=None)
    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.db_client.get_user_by_id",
        AsyncMock(return_value=fake_user),
    ):
        result = await resolve_assigned_caller_id("client:rep-42")
    assert result is None


async def test_resolve_assigned_caller_id_returns_phone_number_on_match(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"phone_number": "+15559998888"}]
    fake_response.raise_for_status = MagicMock()

    fake_http_client = AsyncMock()
    fake_http_client.get = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.db_client.get_user_by_id",
        AsyncMock(return_value=fake_user),
    ), patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.httpx.AsyncClient",
        return_value=fake_http_client,
    ):
        result = await resolve_assigned_caller_id("client:rep-42")

    assert result == "+15559998888"
