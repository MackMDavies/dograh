"""Authentication for the live-audio tap.

/sw-tap was unauthenticated in production for months: anyone who guessed a call id could
open the socket and push audio frames into whatever a manager was listening to.

The obvious fix -- set SIGNALWIRE_WEBHOOK_KEY, which _secret_ok() already checks -- took
ALL calling down within seconds when it was tried, because that same secret guards five
HTTP handlers whose URLs are configured in the SignalWire DASHBOARD and carry no `k`.
Every SWML fetch was rejected and every call became ring-then-hangup.

So the invariant these tests exist to hold is not "the tap is authenticated". It is: the
tap is authenticated WITHOUT anyone having to set SIGNALWIRE_WEBHOOK_KEY, and the issuing
and checking sides can never disagree about whether a credential is expected.
"""
import os
from unittest.mock import patch

from api.services.telephony.dialer.signalwire_routes import (
    _tap_token,
    _tap_websocket_url,
)

_ENDPOINT = "https://api.example.com"


def _env(**kw):
    """Replace the SignalWire secrets outright, so the host's real env cannot leak in."""
    base = {"SIGNALWIRE_WEBHOOK_KEY": "", "SIGNALWIRE_API_TOKEN": ""}
    base.update(kw)
    return patch.dict(os.environ, base, clear=False)


class TestTapToken:
    def test_derives_from_the_api_token_alone(self):
        # THE POINT OF THE WHOLE CHANGE. SIGNALWIRE_API_TOKEN is necessarily present on
        # any box that can place a call, so the tap is authenticated out of the box and
        # nobody is tempted to reach for the variable that breaks calling.
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            assert _tap_token("call-1") != ""

    def test_prefers_the_webhook_key_when_someone_has_set_it(self):
        with _env(SIGNALWIRE_API_TOKEN="tok", SIGNALWIRE_WEBHOOK_KEY="key"):
            preferred = _tap_token("call-1")
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            fallback = _tap_token("call-1")
        assert preferred != fallback

    def test_is_per_call(self):
        # A leaked token buys one call id while that call is live, not every call forever.
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            assert _tap_token("call-1") != _tap_token("call-2")

    def test_is_stable_for_one_call(self):
        # Issuing and checking are separate invocations; they must agree.
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            assert _tap_token("call-1") == _tap_token("call-1")

    def test_does_not_leak_the_secret(self):
        with _env(SIGNALWIRE_API_TOKEN="super-secret-value"):
            assert "super-secret-value" not in _tap_token("call-1")

    def test_no_secret_means_no_token(self):
        # Only reachable on a box with no SignalWire credentials, which cannot place
        # calls. Returning a constant here would be worse than returning nothing.
        with _env():
            assert _tap_token("call-1") == ""

    def test_no_call_id_means_no_token(self):
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            assert _tap_token("") == ""


class TestTapUrl:
    def test_carries_the_token(self):
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            url = _tap_websocket_url(_ENDPOINT, "call-1")
            assert f"t={_tap_token('call-1')}" in url

    def test_is_still_a_websocket_url_for_our_own_host(self):
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            url = _tap_websocket_url(_ENDPOINT, "call-1")
        assert url.startswith("wss://api.example.com/api/v1/telephony/sw-tap?")
        assert "call_id=call-1" in url

    def test_issuing_and_checking_agree_by_construction(self):
        # The failure that caused the outage was a check whose counterpart lived
        # somewhere else and could not be enabled with it. Here the URL carries a token
        # exactly when one is expected, because one function decides both.
        for env in ({}, {"SIGNALWIRE_API_TOKEN": "tok"}):
            with _env(**env):
                url = _tap_websocket_url(_ENDPOINT, "call-1")
                expected = _tap_token("call-1")
                assert ("&t=" in url) == bool(expected)

    def test_kill_switch_still_wins(self):
        # The tap rides every live call; turning it off must not need a code change.
        with _env(SIGNALWIRE_API_TOKEN="tok", SYSEVO_DIALER_TAP="off"):
            assert _tap_websocket_url(_ENDPOINT, "call-1") == ""

    def test_no_endpoint_means_no_tap_rather_than_a_broken_url(self):
        with _env(SIGNALWIRE_API_TOKEN="tok"):
            assert _tap_websocket_url("", "call-1") == ""
