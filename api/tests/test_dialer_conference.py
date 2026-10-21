"""Unit tests for dialer_conference.py - Conference naming + outbound lead dial."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from api.services.telephony.providers.twilio.dialer_conference import (
    cancel_call,
    cancel_call_if_ringing,
    conference_name_for,
    dial_lead_into_conference,
    parent_call_sid_from_conference_name,
)


@pytest.fixture(autouse=True)
def isolate_platform_credentials(monkeypatch):
    from api.db import db_client

    monkeypatch.setattr(
        db_client,
        "get_platform_dialer_credentials",
        AsyncMock(return_value={"account_sid":"ACtest","auth_token":"test-token"}),
    )


def test_conference_name_for():
    assert conference_name_for("CA111") == "call-CA111"


def test_parent_call_sid_from_conference_name_round_trips():
    assert parent_call_sid_from_conference_name(conference_name_for("CA111")) == "CA111"


def test_parent_call_sid_from_conference_name_returns_none_for_unrecognized_name():
    assert parent_call_sid_from_conference_name("some-other-conference") is None


async def test_dial_lead_into_conference_returns_none_without_credentials(monkeypatch):
    from api.db import db_client
    monkeypatch.setattr(db_client, "get_platform_dialer_credentials", AsyncMock(return_value=None))
    monkeypatch.delenv("SYSEVO_TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("SYSEVO_TWILIO_AUTH_TOKEN", raising=False)
    result = await dial_lead_into_conference(
        parent_call_sid="CA111",
        lead_number="+15559876543",
        caller_id="+15551234567",
        join_conference_url="https://api.example.com/api/v1/telephony/dialer-conference-join",
        status_callback_url="https://api.example.com/api/v1/telephony/dialer-call-status",
    )
    assert result is None


async def test_dial_lead_into_conference_returns_call_sid_on_success(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")

    fake_call = MagicMock(sid="CA999")
    fake_client = MagicMock()
    fake_client.calls.create.return_value = fake_call

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await dial_lead_into_conference(
            parent_call_sid="CA111",
            lead_number="+15559876543",
            caller_id="+15551234567",
            join_conference_url="https://api.example.com/api/v1/telephony/dialer-conference-join",
            status_callback_url="https://api.example.com/api/v1/telephony/dialer-call-status",
        )

    assert result == "CA999"
    fake_client.calls.create.assert_called_once_with(
        to="+15559876543",
        from_="+15551234567",
        url="https://api.example.com/api/v1/telephony/dialer-conference-join",
        method="POST",
        status_callback="https://api.example.com/api/v1/telephony/dialer-call-status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
    )


async def test_dial_lead_only_enables_message_end_detection_when_requested(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")
    fake_client = MagicMock()
    fake_client.calls.create.return_value = MagicMock(sid="CA999")
    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await dial_lead_into_conference(
            parent_call_sid="CA111",
            lead_number="+15559876543",
            caller_id="+15551234567",
            join_conference_url="https://api.example.com/join",
            status_callback_url="https://api.example.com/status",
            machine_detection=True,
        )

    assert result == "CA999"
    params = fake_client.calls.create.call_args.kwargs
    assert params["machine_detection"] == "DetectMessageEnd"
    assert params["machine_detection_timeout"] == 16  # balanced: a long limit records silence into the mailbox
    assert params["machine_detection_speech_threshold"] == 2400
    assert params["machine_detection_speech_end_threshold"] == 1200
    assert params["machine_detection_silence_timeout"] == 5000


async def test_parallel_leg_ring_timeout_is_passed_to_twilio(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")
    fake_client = MagicMock()
    fake_client.calls.create.return_value = MagicMock(sid="CA999")
    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await dial_lead_into_conference(
            parent_call_sid="CA111",
            lead_number="+15559876543",
            caller_id="+15551234567",
            join_conference_url="https://api.example.com/join",
            status_callback_url="https://api.example.com/status",
            ringing_timeout_seconds=30,
        )

    assert result == "CA999"
    assert fake_client.calls.create.call_args.kwargs["timeout"] == 30


async def test_dial_lead_uses_active_platform_account_over_environment(monkeypatch):
    """The server REST leg must use the account that minted the browser token."""
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACother-account")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "other-token")

    from api.db import db_client

    monkeypatch.setattr(
        db_client,
        "get_platform_dialer_credentials",
        AsyncMock(
            return_value={
                "account_sid": "ACsysevo-account",
                "auth_token": "sysevo-token",
                "api_key_sid": "SKsysevo-key",
                "api_key_secret": "sysevo-secret",
                "twiml_app_sid": "APsysevo-app",
                "default_caller_id": "+15551234567",
            }
        ),
    )
    fake_client = MagicMock()
    fake_client.calls.create.return_value = MagicMock(sid="CA999")

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ) as client_factory:
        result = await dial_lead_into_conference(
            parent_call_sid="CA111",
            lead_number="+15559876543",
            caller_id="+15551234567",
            join_conference_url="https://api.example.com/api/v1/telephony/dialer-conference-join",
            status_callback_url="https://api.example.com/api/v1/telephony/dialer-call-status",
        )

    assert result == "CA999"
    assert client_factory.call_args.args[:2] == ("ACsysevo-account", "sysevo-token")


async def test_dial_lead_into_conference_returns_none_on_exception(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")

    fake_client = MagicMock()
    fake_client.calls.create.side_effect = Exception("Twilio down")

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await dial_lead_into_conference(
            parent_call_sid="CA111",
            lead_number="+15559876543",
            caller_id="+15551234567",
            join_conference_url="https://api.example.com/api/v1/telephony/dialer-conference-join",
            status_callback_url="https://api.example.com/api/v1/telephony/dialer-call-status",
        )

    assert result is None


async def test_cancel_call_completes_the_call(monkeypatch):
    """status="completed", not "canceled": "canceled" is only valid for a
    queued/ringing call, and a lead who answers in the window between the rep
    hanging up and this landing would be in-progress - the exact case where
    failing to end the leg produces the abandoned call this prevents."""
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")

    fake_call_context = MagicMock()
    fake_client = MagicMock()
    fake_client.calls.return_value = fake_call_context

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await cancel_call(call_sid="CA222")

    assert result is True
    fake_client.calls.assert_called_once_with("CA222")
    fake_call_context.update.assert_called_once_with(status="completed")


async def test_cancel_call_returns_false_without_credentials(monkeypatch):
    from api.db import db_client
    monkeypatch.setattr(db_client, "get_platform_dialer_credentials", AsyncMock(return_value=None))
    monkeypatch.delenv("SYSEVO_TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("SYSEVO_TWILIO_AUTH_TOKEN", raising=False)
    assert await cancel_call(call_sid="CA222") is False


async def test_cancel_call_returns_false_on_exception(monkeypatch):
    """A Twilio rejection (e.g. the leg is already terminal) must never
    surface as a 500 from the webhook that called this."""
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")

    fake_call_context = MagicMock()
    fake_call_context.update.side_effect = Exception("Call is not in-progress")
    fake_client = MagicMock()
    fake_client.calls.return_value = fake_call_context

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        assert await cancel_call(call_sid="CA222") is False


async def test_cancel_call_if_ringing_only_cancels_provider_ringing_leg(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")
    fake_call_context = MagicMock()
    fake_call_context.fetch.return_value = MagicMock(status="ringing")
    fake_client = MagicMock()
    fake_client.calls.return_value = fake_call_context

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await cancel_call_if_ringing(call_sid="CA222")

    assert result is True
    fake_call_context.update.assert_called_once_with(status="canceled")


async def test_cancel_call_if_ringing_preserves_answered_human(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-token")
    fake_call_context = MagicMock()
    fake_call_context.fetch.return_value = MagicMock(status="in-progress")
    fake_client = MagicMock()
    fake_client.calls.return_value = fake_call_context

    with patch(
        "api.services.telephony.providers.twilio.dialer_conference.Client",
        return_value=fake_client,
    ):
        result = await cancel_call_if_ringing(call_sid="CA222")

    assert result is False
    fake_call_context.update.assert_not_called()


async def test_dialer_never_falls_back_to_shared_ai_account(monkeypatch):
    from api.db import db_client
    from api.services.telephony.providers.twilio.dialer_conference import _twilio_client
    monkeypatch.setattr(db_client, "get_platform_dialer_credentials", AsyncMock(return_value=None))
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "ACunrelated-ai")
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "unrelated-token")
    assert await _twilio_client() is None
