"""Unit tests for the dialer provider abstraction."""

import pytest

from api.services.telephony.dialer.provider import (
    DialerCredentials,
    UnknownDialerProvider,
    get_dialer_provider,
)


def test_get_dialer_provider_returns_twilio():
    p = get_dialer_provider("twilio")
    assert p.name == "twilio"


def test_get_dialer_provider_returns_signalwire():
    p = get_dialer_provider("signalwire")
    assert p.name == "signalwire"


def test_get_dialer_provider_is_case_insensitive_and_trims():
    assert get_dialer_provider("  TWILIO ").name == "twilio"


def test_get_dialer_provider_rejects_unknown():
    with pytest.raises(UnknownDialerProvider):
        get_dialer_provider("vonage")


def test_get_dialer_provider_rejects_empty():
    with pytest.raises(UnknownDialerProvider):
        get_dialer_provider("")


def test_dialer_credentials_is_a_plain_container():
    c = DialerCredentials(token="t", identity="rep-1", destination="/private/dialer")
    assert (c.token, c.identity, c.destination) == ("t", "rep-1", "/private/dialer")
