"""Unit tests for dialer_number_assignment.py - per-rep caller ID resolution."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.services.telephony.providers.twilio.dialer_number_assignment import (
    DialerNumberAssignmentUnavailable,
    DialerNumberProviderMismatch,
    _parse_rep_id_from_identity,
    choose_parallel_caller_ids,
    is_manager_or_admin,
    parallel_caller_id_candidates,
    resolve_assigned_caller_id,
    resolve_assigned_dialer_number,
)


def test_choose_parallel_caller_ids_prefers_local_area_and_uses_distinct_dids():
    targets = [
        {"entry_id": "lead-602", "phone": "+1 (602) 555-0101"},
        {"entry_id": "lead-404", "phone": "+1 404 555 0102"},
        {"entry_id": "lead-602b", "phone": "+1 602 555 0103"},
    ]
    numbers = ["+1 404 555 0199", "+1 602 555 0198", "+1 212 555 0197"]

    selected = choose_parallel_caller_ids(targets, numbers, "+1 415 555 0100")

    assert selected == {
        "lead-602": "+16025550198",
        "lead-404": "+14045550199",
        "lead-602b": "+12125550197",
    }


def test_choose_parallel_caller_ids_falls_back_when_roster_is_exhausted():
    selected = choose_parallel_caller_ids(
        [
            {"entry_id": "one", "phone": "+1 602 555 0101"},
            {"entry_id": "two", "phone": "+1 404 555 0102"},
        ],
        ["+1 602 555 0198"],
        "+1 415 555 0100",
    )

    assert selected == {"one": "+16025550198", "two": "+14155550100"}


async def test_parallel_caller_candidates_exclude_numbers_from_other_twilio_accounts(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    user = MagicMock(provider_id="00000000-0000-0000-0000-000000000001")
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = [
        {"phone_number": "+16025550198", "twilio_account_sid": "AC_ACTIVE"},
        {"phone_number": "+14045550199", "twilio_account_sid": "AC_OTHER"},
        {"phone_number": "+14155550100", "twilio_account_sid": None},
    ]
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.db_client.get_user_by_id",
        AsyncMock(return_value=user),
    ), patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.db_client.get_platform_dialer_credentials",
        AsyncMock(return_value={"account_sid": "AC_ACTIVE"}),
    ), patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.httpx.AsyncClient",
        return_value=client,
    ):
        result = await parallel_caller_id_candidates(42, "+14155550100")

    assert result == ["+16025550198", "+14155550100"]


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


async def test_resolve_assigned_caller_id_returns_phone_number_on_match():
    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.resolve_assigned_dialer_number",
        AsyncMock(return_value={"phone_number": "+15559998888", "provider": "twilio"}),
    ):
        result = await resolve_assigned_caller_id("client:rep-42")

    assert result == "+15559998888"


async def test_resolve_assigned_caller_id_fails_closed_on_lookup_error():
    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.resolve_assigned_dialer_number",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    ):
        with pytest.raises(DialerNumberAssignmentUnavailable):
            await resolve_assigned_caller_id("client:rep-42")


async def test_resolve_assigned_caller_id_rejects_a_different_provider():
    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.resolve_assigned_dialer_number",
        AsyncMock(return_value={"phone_number": "+15559998888", "provider": "telnyx"}),
    ):
        with pytest.raises(DialerNumberProviderMismatch, match="Active dialer line belongs to telnyx"):
            await resolve_assigned_caller_id("client:rep-42", "signalwire")


async def test_resolve_assigned_dialer_number_uses_rotation_rpc(monkeypatch):
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
    fake_response.json.return_value = [{
        "phone_number": "+15559998888", "provider": "telnyx",
        "next_rotation_at": "2026-10-05T00:00:00+00:00",
    }]
    fake_response.raise_for_status = MagicMock()
    fake_http_client = AsyncMock()
    fake_http_client.post = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.db_client.get_user_by_id",
        AsyncMock(return_value=fake_user),
    ), patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.httpx.AsyncClient",
        return_value=fake_http_client,
    ):
        row = await resolve_assigned_dialer_number(42)
    assert row["provider"] == "telnyx"
    assert row["next_rotation_at"] == "2026-10-05T00:00:00+00:00"
    fake_http_client.post.assert_awaited_once()
    assert fake_http_client.post.await_args.args[0].endswith("/rpc/dialer_current_assigned_number")


async def test_is_manager_or_admin_returns_true_for_manager(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = [{"role": "sales_manager"}]
    fake_http_client = AsyncMock()
    fake_http_client.get = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.httpx.AsyncClient",
        return_value=fake_http_client,
    ):
        result = await is_manager_or_admin("00000000-0000-0000-0000-000000000001")

    assert result is True


async def test_is_manager_or_admin_returns_false_for_no_matching_role(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = []
    fake_http_client = AsyncMock()
    fake_http_client.get = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.httpx.AsyncClient",
        return_value=fake_http_client,
    ):
        result = await is_manager_or_admin("00000000-0000-0000-0000-000000000001")

    assert result is False


async def test_is_manager_or_admin_fails_closed_on_error(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_number_assignment.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_http_client = AsyncMock()
    fake_http_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_number_assignment.httpx.AsyncClient",
        return_value=fake_http_client,
    ):
        result = await is_manager_or_admin("00000000-0000-0000-0000-000000000001")

    assert result is False
