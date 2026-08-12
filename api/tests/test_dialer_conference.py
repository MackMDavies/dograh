"""Unit tests for dialer_conference.py - Conference naming + outbound lead dial."""
from unittest.mock import MagicMock, patch

from api.services.telephony.providers.twilio.dialer_conference import (
    cancel_call,
    conference_name_for,
    dial_lead_into_conference,
    parent_call_sid_from_conference_name,
)


def test_conference_name_for():
    assert conference_name_for("CA111") == "call-CA111"


def test_parent_call_sid_from_conference_name_round_trips():
    assert parent_call_sid_from_conference_name(conference_name_for("CA111")) == "CA111"


def test_parent_call_sid_from_conference_name_returns_none_for_unrecognized_name():
    assert parent_call_sid_from_conference_name("some-other-conference") is None


async def test_dial_lead_into_conference_returns_none_without_credentials(monkeypatch):
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
