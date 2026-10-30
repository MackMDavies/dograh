"""Unit tests for dialer_call_log.py - Supabase writes for the dialer call log."""
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from loguru import logger

from api.services.telephony.providers.twilio.dialer_call_log import (
    _affected_row_count,
    create_dialer_call,
    consume_double_dial_authorization,
    check_dialer_calling_hours,
    check_dialer_calling_hours_with_state,
    get_auto_voicemail_settings,
    get_dialer_call_child_leg,
    update_dialer_call_child_sid,
    update_dialer_call_conference_sid,
    update_dialer_call_recording_by_conference_sid,
    update_dialer_call_recording_url,
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
    monkeypatch.setattr(
        "api.services.telephony.dialer.compliance.SUPABASE_URL",
        "https://example.supabase.co",
    )
    monkeypatch.setattr(
        "api.services.telephony.dialer.compliance.SUPABASE_SERVICE_ROLE_KEY",
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


async def test_check_dialer_calling_hours_posts_phone_and_entry(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"allowed": True, "timezone": "America/New_York"}
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        allowed, reason = await check_dialer_calling_hours("+12125551234", "entry-123")

    assert (allowed, reason) == (True, None)
    call = fake_client.post.await_args
    assert call.args[0] == "https://example.supabase.co/functions/v1/dialer-calling-hours-check"
    assert call.kwargs["json"] == {"phone": "+12125551234", "entry_id": "entry-123"}
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-service-role-key"


async def test_check_dialer_calling_hours_with_state_returns_server_state(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "allowed": True,
        "timezone": "America/Los_Angeles",
        "us_state": "CA",
    }
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        result = await check_dialer_calling_hours_with_state("+14155551234")

    assert result == (True, None, "CA")


async def test_check_dialer_calling_hours_fails_closed_on_block(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"allowed": False, "reason": "outside_calling_hours"}
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        allowed, reason = await check_dialer_calling_hours("+442071234567")

    assert (allowed, reason) == (False, "outside_calling_hours")


async def test_check_dialer_calling_hours_fails_closed_when_service_unavailable(monkeypatch):
    _configure_supabase(monkeypatch)
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        side_effect=httpx.ConnectError("offline"),
    ):
        allowed, reason = await check_dialer_calling_hours("+442071234567")

    assert (allowed, reason) == (False, "compliance_unavailable")


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
        # Defaulted, not passed by the Twilio call site - the default is what
        # keeps every existing caller correct without an edit.
        "provider": "twilio",
    }


async def test_create_dialer_call_records_an_explicit_provider(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await create_dialer_call(
            parent_call_sid="sw-1",
            rep_user_id="00000000-0000-0000-0000-000000000001",
            entry_id=None,
            from_number="+12092669253",
            to_number="+14155550123",
            provider="signalwire",
        )

    assert fake_client.post.await_args.kwargs["json"]["provider"] == "signalwire"


async def test_create_dialer_call_links_authorized_double_dial(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await create_dialer_call(
            parent_call_sid="CA" + "1" * 32,
            rep_user_id="00000000-0000-0000-0000-000000000001",
            entry_id="00000000-0000-0000-0000-000000000002",
            from_number="+15551234567",
            to_number="+15559876543",
            double_dial_retry_of="00000000-0000-0000-0000-000000000003",
            double_dial_authorization_id="00000000-0000-0000-0000-000000000004",
        )

    payload = fake_client.post.await_args.kwargs["json"]
    assert payload["double_dial_retry_of"] == "00000000-0000-0000-0000-000000000003"
    assert payload["double_dial_authorization_id"] == "00000000-0000-0000-0000-000000000004"


async def test_consume_double_dial_authorization_is_fail_closed(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = [{
        "from_number": "+15551234567",
        "retry_of_call_id": "00000000-0000-0000-0000-000000000003",
    }]
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        result = await consume_double_dial_authorization(
            authorization_id="00000000-0000-0000-0000-000000000004",
            rep_user_id="00000000-0000-0000-0000-000000000001",
            entry_id="00000000-0000-0000-0000-000000000002",
            to_number="+15559876543",
            call_sid="CA" + "1" * 32,
        )

    assert result == {
        "from_number": "+15551234567",
        "retry_of_call_id": "00000000-0000-0000-0000-000000000003",
    }
    assert fake_client.post.await_args.args[0].endswith(
        "/rpc/dialer_consume_double_dial_authorization"
    )


async def test_update_dialer_call_recording_url_patches_by_parent_call_sid(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_recording_url(
            parent_call_sid="sw-1", recording_url="https://rec.example/a.mp3"
        )

    call = fake_client.patch.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {"parent_call_sid": "eq.sw-1", "select": "id"}
    # recording_url, not recording_sid: SignalWire hands back a playable URL,
    # where Twilio's SID has to go through the recording proxy.
    assert call.kwargs["json"] == {"recording_url": "https://rec.example/a.mp3"}


async def test_update_dialer_call_recording_url_swallows_errors(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = AsyncMock()
    fake_client.patch = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_recording_url(
            parent_call_sid="sw-1", recording_url="https://rec.example/a.mp3"
        )


async def test_update_dialer_call_recording_url_noops_without_supabase(monkeypatch):
    monkeypatch.setattr(
        "api.services.telephony.providers.twilio.dialer_call_log.SUPABASE_SERVICE_ROLE_KEY",
        "",
    )
    fake_client = AsyncMock()

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_recording_url(
            parent_call_sid="sw-1", recording_url="https://rec.example/a.mp3"
        )

    fake_client.patch.assert_not_called()


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


async def _patched_payload(monkeypatch, **kwargs) -> dict:
    """Run update_dialer_call_status and hand back the JSON body it would PATCH."""
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
        await update_dialer_call_status(**kwargs)

    return fake_client.patch.await_args.kwargs["json"]


async def test_update_dialer_call_status_omits_ended_at_while_the_call_is_being_set_up(
    monkeypatch,
):
    """A setup status is not an ending, and the key is ABSENT rather than null.

    Absent and null are different instructions to PostgREST: absent leaves the column
    alone, null erases it. This used to send null, which is why a 'ringing' callback
    arriving after a real end time had been recorded could wipe it.
    """
    payload = await _patched_payload(
        monkeypatch,
        parent_call_sid="CA111",
        child_call_sid="CA222",
        status="ringing",
        duration_seconds=None,
    )

    assert "ended_at" not in payload
    # Nothing is known about the duration on this callback, so nothing is said about it.
    assert "duration_seconds" not in payload


@pytest.mark.parametrize("status", ["completed", "busy", "failed", "no-answer", "canceled"])
async def test_update_dialer_call_status_stamps_an_end_time_for_every_ending(
    monkeypatch, status
):
    """The regression, and it was four fifths of all endings.

    Only 'completed' used to get an end time. Every cancelled, busy, failed or
    unanswered call therefore settled in Supabase with a terminal status and no
    ended_at -- and anything asking `ended_at is null` read those as calls in
    progress. A manager's Live tab showed two calls that had hung up half an hour
    earlier, with running timers and a Listen button on each.
    """
    payload = await _patched_payload(
        monkeypatch,
        parent_call_sid="CA111",
        child_call_sid="CA222",
        status=status,
        duration_seconds=7,
    )

    assert payload["ended_at"] is not None
    assert payload["status"] == status


async def test_update_dialer_call_status_never_erases_what_it_does_not_know(monkeypatch):
    """A null in a PATCH is an instruction to erase, so unknowns are omitted.

    The old body sent every key on every callback. A 'no-answer' carrying no duration
    therefore blanked a real duration another callback had already written, and the
    same was true of child_call_sid.
    """
    payload = await _patched_payload(
        monkeypatch,
        parent_call_sid="CA111",
        child_call_sid=None,
        status="no-answer",
        duration_seconds=None,
    )

    assert "child_call_sid" not in payload
    assert "duration_seconds" not in payload
    # ...but the ending itself is known, and is still recorded.
    assert payload["status"] == "no-answer"
    assert payload["ended_at"] is not None


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


def _patch_response(*, content_range: str = "0-0/1") -> MagicMock:
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.headers = {"Content-Range": content_range}
    return fake_response


@contextmanager
def _captured_warning_logs():
    """loguru doesn't feed pytest's caplog, so grab a sink directly."""
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.mark.parametrize(
    "content_range,expected",
    [
        ("0-0/1", 1),
        ("0-4/5", 5),
        ("*/0", 0),
        ("*/*", None),
        ("", None),
        ("garbage", None),
    ],
)
def test_affected_row_count_reads_postgrest_content_range(content_range, expected):
    """None means "PostgREST didn't say", which must never be mistaken for 0 -
    that's the difference between a silent miss and an unreadable header."""
    assert _affected_row_count(content_range) == expected


async def test_update_dialer_call_recording_by_conference_sid_patches_by_conference_sid(
    monkeypatch,
):
    """The whole point of this function: a Conference recording's callback
    knows the ConferenceSid and nothing else, so the WHERE clause must be
    conference_sid - matching on parent_call_sid would silently update zero
    rows."""
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_patch_response())

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_recording_by_conference_sid(
            conference_sid="CF999", recording_sid="RE999"
        )

    fake_client.patch.assert_awaited_once()
    call = fake_client.patch.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {"conference_sid": "eq.CF999"}
    assert call.kwargs["json"] == {"recording_sid": "RE999"}
    # Without count=exact PostgREST answers a zero-row PATCH with a bare 204
    # and the miss is invisible.
    assert call.kwargs["headers"]["Prefer"] == "count=exact"
    # The Prefer header is an ADDITION to the module's auth headers, not a
    # replacement - dropping these would 401 rather than update anything.
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-service-role-key"
    assert call.kwargs["headers"]["Content-Type"] == "application/json"


async def test_update_dialer_call_recording_by_conference_sid_warns_when_no_row_matched(
    monkeypatch,
):
    """A PATCH matching nothing is a healthy 204, so raise_for_status can't
    see it. Unwarned, an orphaned recording looks exactly like a successful
    one: no error in Dograh, Twilio or Supabase, just a call with no play
    button that only a human ever notices."""
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_patch_response(content_range="*/0"))

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ), _captured_warning_logs() as messages:
        await update_dialer_call_recording_by_conference_sid(
            conference_sid="CF999", recording_sid="RE999"
        )

    assert any("matched 0 rows" in m and "CF999" in m and "RE999" in m for m in messages)


async def test_update_dialer_call_recording_by_conference_sid_quiet_when_row_matched(
    monkeypatch,
):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_patch_response(content_range="0-0/1"))

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ), _captured_warning_logs() as messages:
        await update_dialer_call_recording_by_conference_sid(
            conference_sid="CF999", recording_sid="RE999"
        )

    assert messages == []


async def test_update_dialer_call_recording_by_conference_sid_quiet_when_count_unreadable(
    monkeypatch,
):
    """An unreadable Content-Range is "don't know", not "matched nothing" -
    crying wolf on every update would train the warning to be ignored."""
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_patch_response(content_range="*/*"))

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ), _captured_warning_logs() as messages:
        await update_dialer_call_recording_by_conference_sid(
            conference_sid="CF999", recording_sid="RE999"
        )

    assert messages == []


async def test_update_dialer_call_recording_by_conference_sid_swallows_errors(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = AsyncMock()
    fake_client.patch = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await update_dialer_call_recording_by_conference_sid(
            conference_sid="CF999", recording_sid="RE999"
        )


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
        return_value=[{"child_call_sid": "CA222", "status": "ringing", "rep_user_id": "user-1"}]
    )
    fake_client = _fake_client(fake_response)

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        row = await get_dialer_call_child_leg(parent_call_sid="CA111")

    assert row == {"child_call_sid": "CA222", "status": "ringing", "rep_user_id": "user-1"}
    call = fake_client.get.await_args
    assert call.args[0] == "https://example.supabase.co/rest/v1/dialer_calls"
    assert call.kwargs["params"] == {
        "select": "child_call_sid,status,rep_user_id,provider,amd_result,manual_voicemail_requested_at,voicemail_drop_started_at,auto_voicemail_sent,voicemail_drop_id,ended_at,lead_joined_at",
        "parent_call_sid": "eq.CA111",
        "limit": "1",
    }


def _json_response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=payload)
    return response


_REP = "11111111-1111-4111-8111-111111111111"
_MANAGER = "22222222-2222-4222-8222-222222222222"
_DROP = "33333333-3333-4333-8333-333333333333"


async def test_get_auto_voicemail_settings_uses_team_policy_and_server_resolver(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response({}))
    fake_client.post.side_effect = [
        _json_response({
            "auto_drop_enabled": True,
            "machine_detection_enabled": True,
            "machine_detection_preset": "balanced",
            "on_machine": "drop_vm",
        }),
        _json_response({
            "drop_id": _DROP, "storage_path": f"{_REP}/message.wav", "duration_seconds": 24,
            "owner_user_id": _REP, "source": "own_default",
        }),
        _json_response({"signedURL": f"/object/sign/voicemail-drops/{_REP}/message.wav?token=abc"}),
    ]

    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ), patch(
        "api.services.telephony.providers.twilio.dialer_call_log.verify_voicemail_audio",
        new=AsyncMock(return_value=True),
    ) as verify:
        enabled, url, drop_id = await get_auto_voicemail_settings(
            user_id=_REP, parent_call_sid="CA111", entry_id="entry-1"
        )

    assert enabled is True
    assert drop_id == _DROP
    assert url == f"https://example.supabase.co/storage/v1/object/sign/voicemail-drops/{_REP}/message.wav?token=abc"
    verify.assert_awaited_once_with(url)
    policy_call, resolver_call, sign_call = fake_client.post.await_args_list
    assert policy_call.args[0].endswith("/rest/v1/rpc/dialer_account_voicemail_policy")
    assert policy_call.kwargs["json"] == {
        "p_rep_user_id": _REP, "p_entry_id": "entry-1", "p_parent_call_sid": "CA111",
    }
    assert resolver_call.args[0].endswith("/rest/v1/rpc/dialer_effective_voicemail_drop")
    assert resolver_call.kwargs["json"] == {
        "p_rep_user_id": _REP, "p_entry_id": "entry-1", "p_parent_call_sid": "CA111",
    }
    assert sign_call.args[0].endswith(f"/storage/v1/object/sign/voicemail-drops/{_REP}/message.wav")
    assert sign_call.kwargs["json"] == {"expiresIn": 1800}
    from api.services.telephony.providers.twilio.dialer_call_log import voicemail_duration
    assert voicemail_duration(_DROP) == 24.0


async def test_get_auto_voicemail_settings_plays_the_team_default_for_a_rep_without_one(monkeypatch):
    """Organisation fallback: the path lives in the MANAGER's folder, owned by them."""
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response({}))
    fake_client.post.side_effect = [
        _json_response({"auto_drop_enabled": True, "on_machine": "drop_vm"}),
        _json_response({
            "drop_id": _DROP, "storage_path": f"{_MANAGER}/team.wav", "duration_seconds": 22,
            "owner_user_id": _MANAGER, "source": "team_default",
        }),
        _json_response({"signedURL": f"/object/sign/voicemail-drops/{_MANAGER}/team.wav?token=t"}),
    ]
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ), patch(
        "api.services.telephony.providers.twilio.dialer_call_log.verify_voicemail_audio",
        new=AsyncMock(return_value=True),
    ):
        enabled, url, drop_id = await get_auto_voicemail_settings(user_id=_REP, parent_call_sid="CA111")
    assert enabled is True and drop_id == _DROP
    assert url.endswith(f"/voicemail-drops/{_MANAGER}/team.wav?token=t")


@pytest.mark.parametrize("row", [
    # A path outside its owner's folder must never be signed.
    {"drop_id": _DROP, "storage_path": "another-user/message.wav", "owner_user_id": _REP},
    {"drop_id": _DROP, "storage_path": f"{_REP}/../x.wav", "owner_user_id": _REP},
    {"drop_id": "not-a-uuid", "storage_path": f"{_REP}/message.wav", "owner_user_id": _REP},
    None,
])
async def test_get_auto_voicemail_settings_fails_closed_for_unsafe_or_missing_recording(monkeypatch, row):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response({}))
    fake_client.post.side_effect = [
        _json_response({"auto_drop_enabled": True}),
        _json_response(row),
    ]
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        enabled, url, drop_id = await get_auto_voicemail_settings(user_id=_REP, parent_call_sid="CA111")
    assert (enabled, url, drop_id) == (False, None, None)
    assert fake_client.post.await_count == 2  # nothing was signed


async def test_resolver_falls_back_to_own_default_before_the_migration_is_applied(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response({}))
    fake_client.post.return_value = _json_response({"code": "PGRST202"}, status_code=404)
    fake_client.get.return_value = _json_response([
        {"id": _DROP, "storage_path": f"{_REP}/message.wav", "duration_seconds": 19},
    ])
    from api.services.telephony.providers.twilio.dialer_call_log import resolve_effective_voicemail_drop
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        drop = await resolve_effective_voicemail_drop(rep_user_id=_REP)
    assert drop["id"] == _DROP and drop["source"] == "own_default"
    assert fake_client.get.await_args.kwargs["params"]["user_id"] == f"eq.{_REP}"


async def test_get_auto_voicemail_settings_refuses_a_file_twilio_cannot_play(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response({}))
    fake_client.post.side_effect = [
        _json_response({"auto_drop_enabled": True}),
        _json_response({"drop_id": _DROP, "storage_path": f"{_REP}/m.wav", "owner_user_id": _REP}),
        _json_response({"signedURL": f"/object/sign/voicemail-drops/{_REP}/m.wav?token=x"}),
    ]
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ), patch(
        "api.services.telephony.providers.twilio.dialer_call_log.verify_voicemail_audio",
        new=AsyncMock(return_value=False),
    ):
        assert await get_auto_voicemail_settings(user_id=_REP) == (False, None, None)


class _FakeStream:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        yield self._body


@pytest.mark.parametrize("status,body,expected", [
    (206, b"RIFF\x24\x00\x00\x00WAVEfmt ", True),
    (200, b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00", True),
    (206, b"\x1aE\xdf\xa3webm-container", False),   # a WebM renamed .wav
    (200, b"<html>error</html>", False),
    (404, b"", False),
    (500, b"", None),
])
async def test_verify_voicemail_audio_checks_the_real_header(status, body, expected):
    from api.services.telephony.providers.twilio.dialer_call_log import verify_voicemail_audio
    client = AsyncMock()
    client.stream = MagicMock(return_value=_FakeStream(status, body))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=client,
    ):
        assert await verify_voicemail_audio("https://example.supabase.co/x.wav?token=t") is expected
    assert client.stream.call_args.kwargs["headers"] == {"Range": "bytes=0-15"}


async def test_record_twilio_amd_decision_posts_measured_duration(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response(True))
    from api.services.telephony.providers.twilio.dialer_call_log import record_twilio_amd_decision
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await record_twilio_amd_decision(parent_call_sid="CA1", decision_ms="2410", leg_id="leg") is True
        assert await record_twilio_amd_decision(parent_call_sid="CA1", decision_ms="n/a") is False
    call = fake_client.post.await_args_list[0]
    assert call.args[0].endswith("/rest/v1/rpc/dialer_record_amd_decision")
    assert call.kwargs["json"] == {
        "p_parent_call_sid": "CA1", "p_decision_ms": 2410, "p_child_call_sid": None, "p_leg_id": "leg",
    }
    assert fake_client.post.await_count == 1


async def test_voicemail_evidence_read_is_optional(monkeypatch):
    _configure_supabase(monkeypatch)
    fake_client = _fake_client(_json_response({}))
    fake_client.get.side_effect = httpx.HTTPError("column does not exist")
    from api.services.telephony.providers.twilio.dialer_call_log import get_voicemail_drop_evidence
    with patch(
        "api.services.telephony.providers.twilio.dialer_call_log.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await get_voicemail_drop_evidence(parent_call_sid="CA1") == {}


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
