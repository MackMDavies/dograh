"""Unit tests for dialer_call_log.py - Supabase writes for the dialer call log."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.services.telephony.providers.twilio.dialer_call_log import (
    create_dialer_call,
    get_dialer_call_child_leg,
    update_dialer_call_child_sid,
    update_dialer_call_conference_sid,
    update_dialer_call_recording,
    update_dialer_call_status,
)


def _configure_supabase(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )


def _fake_client(response: MagicMock) -> AsyncMock:
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=response)
    fake_client.patch = AsyncMock(return_value=response)
    fake_client.get = AsyncMock(return_value=response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    return fake_client


async def test_create_dialer_call_posts_expected_payload(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await create_dialer_call(
            parent_call_sid="CA111",
            rep_user_id="00000000-0000-0000-0000-000000000001",
            entry_id="00000000-0000-0000-0000-000000000002",
            from_number="+15551234567",
            to_number="+15559876543",
        )

    fake_client.post.assert_awaited_once()
    call = fake_client.post.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["json"] == {
        "parent_call_sid": "CA111",
        "rep_user_id": "00000000-0000-0000-0000-000000000001",
        "entry_id": "00000000-0000-0000-0000-000000000002",
        "from_number": "+15551234567",
        "to_number": "+15559876543",
        "status": "initiated",
    }


async def test_create_dialer_call_swallows_errors(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        # Must not raise - a logging failure here can never be allowed to
        # break call setup, since this runs inside the voice-connect webhook.
        await create_dialer_call(
            parent_call_sid="CA111",
            rep_user_id="00000000-0000-0000-0000-000000000001",
            entry_id=None,
            from_number="+15551234567",
            to_number="+15559876543",
        )


async def test_update_dialer_call_status_patches_by_parent_call_sid(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_status(
            parent_call_sid="CA111",
            child_call_sid="CA222",
            status="completed",
            duration_seconds=42,
        )

    fake_client.patch.assert_awaited_once()
    call = fake_client.patch.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {"parent_call_sid": "eq.CA111"}
    assert call.kwargs["json"]["child_call_sid"] == "CA222"
    assert call.kwargs["json"]["status"] == "completed"
    assert call.kwargs["json"]["duration_seconds"] == 42
    assert call.kwargs["json"]["ended_at"] is not None


async def test_update_dialer_call_status_leaves_ended_at_null_when_not_completed(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_status(
            parent_call_sid="CA111", child_call_sid="CA222", status="ringing", duration_seconds=None
        )

    call = fake_client.patch.await_args
    assert call.kwargs["json"]["ended_at"] is None


async def test_update_dialer_call_status_swallows_errors(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_client = AsyncMock()
    fake_client.patch = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_status(
            parent_call_sid="CA111", child_call_sid=None, status="ringing", duration_seconds=None
        )


async def test_update_dialer_call_recording_patches_by_parent_call_sid(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "test-service-role-key",
    )
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_recording(parent_call_sid="CA111", recording_sid="RE999")

    fake_client.patch.assert_awaited_once()
    call = fake_client.patch.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {"parent_call_sid": "eq.CA111"}
    assert call.kwargs["json"] == {"recording_sid": "RE999"}


async def test_update_dialer_call_conference_sid_patches_by_parent_call_sid(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_conference_sid(parent_call_sid="CA111", conference_sid="CF999")

    fake_client.patch.assert_awaited_once()
    call = fake_client.patch.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {"parent_call_sid": "eq.CA111"}
    assert call.kwargs["json"] == {"conference_sid": "CF999"}


async def test_update_dialer_call_conference_sid_swallows_errors(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = AsyncMock()
    fake_client.patch = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_conference_sid(parent_call_sid="CA111", conference_sid="CF999")


async def test_get_dialer_call_child_leg_returns_row(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(
        return_value=[{"child_call_sid": "CA222", "status": "ringing"}]
    )
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        row = await get_dialer_call_child_leg(parent_call_sid="CA111")

    assert row == {"child_call_sid": "CA222", "status": "ringing"}
    call = fake_client.get.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {
        "select": "child_call_sid,status",
        "parent_call_sid": "eq.CA111",
        "limit": "1",
    }


async def test_get_dialer_call_child_leg_returns_none_for_no_rows(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value=[])
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await get_dialer_call_child_leg(parent_call_sid="CA111") is None


async def test_get_dialer_call_child_leg_returns_none_for_error_body(monkeypatch):
    """PostgREST returns a JSON object, not a list, for some errors - the
    "never raises" contract has to hold for a malformed body too."""
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value={"message": "nope"})
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await get_dialer_call_child_leg(parent_call_sid="CA111") is None


async def test_get_dialer_call_child_leg_swallows_errors(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await get_dialer_call_child_leg(parent_call_sid="CA111") is None


async def test_get_dialer_call_child_leg_returns_none_without_service_role_key(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "",
    )
    assert await get_dialer_call_child_leg(parent_call_sid="CA111") is None


async def test_update_dialer_call_child_sid_patches_only_child_call_sid(monkeypatch):
    """Deliberately does NOT write status - this races the lead's own status
    callback, and writing "initiated" over a "ringing" that already landed
    would walk the row backwards."""
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_child_sid(parent_call_sid="CA111", child_call_sid="CA222")

    fake_client.patch.assert_awaited_once()
    call = fake_client.patch.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {"parent_call_sid": "eq.CA111"}
    assert call.kwargs["json"] == {"child_call_sid": "CA222"}


async def test_update_dialer_call_child_sid_swallows_errors(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = AsyncMock()
    fake_client.patch = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_child_sid(parent_call_sid="CA111", child_call_sid="CA222")


@pytest.mark.parametrize("body", [["not-a-dict"], [123], [["nested"]], [None]])
async def test_get_dialer_call_child_leg_returns_none_for_array_of_non_objects(
    monkeypatch, body
):
    """Regression: a 200 carrying a JSON array of non-objects (a proxy or
    captive portal answering with something that isn't ours) must not be
    returned as if it were a row. Handing back a str/int/list breaks this
    function's dict|None contract and blows up in the caller's .get() -
    outside every bit of error handling in this module."""
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value=body)
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await get_dialer_call_child_leg(parent_call_sid="CA111") is None
