"""Saving the dialer's Twilio connection accepts a valid Standard API key.

Standard keys cannot read /Accounts; the key is proven with a Voice API read
instead. The account (auth token) and TwiML App checks are unchanged.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from twilio.base.exceptions import TwilioRestException

import api.routes.admin_telephony as at

ACCOUNT, KEY, APP = "AC" + "1" * 32, "SK" + "2" * 32, "AP" + "3" * 32


class _FakeClient:
    """Twilio Client stand-in: the account client sees everything, a Standard key
    gets 401 on /Accounts but may read calls."""

    def __init__(self, username, password, account_sid=None, *, key_valid=True, voice_url="https://api.sysevo.io/api/v1/telephony/voice-connect"):
        self.is_key = username.startswith("SK")
        self.key_valid = key_valid
        self._voice_url = voice_url
        self.api = SimpleNamespace(accounts=lambda sid: SimpleNamespace(fetch=self._fetch_account))
        self.calls = SimpleNamespace(list=self._list_calls)

    def _fetch_account(self):
        if self.is_key:
            raise TwilioRestException(401, "/Accounts", "Standard keys cannot read Accounts")
        return SimpleNamespace(sid=ACCOUNT)

    def _list_calls(self, limit=None):
        if self.is_key and not self.key_valid:
            raise TwilioRestException(401, "/Calls", "Authenticate")
        return []

    def applications(self, sid):
        return SimpleNamespace(fetch=lambda: SimpleNamespace(voice_url=self._voice_url, voice_method="POST"))


def _wire(monkeypatch, **client_kwargs):
    monkeypatch.setattr(at, "Client", lambda u, p, account_sid=None: _FakeClient(u, p, account_sid, **client_kwargs))
    save = AsyncMock()
    monkeypatch.setattr(at.db_client, "get_platform_dialer_credentials", AsyncMock(return_value={}))
    monkeypatch.setattr(at.db_client, "save_platform_dialer_twilio_credentials", save)
    monkeypatch.setattr(at.db_client, "get_platform_dialer_twilio_connection", AsyncMock(return_value={"ok": True}))
    return save


def _body():
    return at.DialerTwilioConnectionRequest(account_sid=ACCOUNT, api_key_sid=KEY, twiml_app_sid=APP, auth_token="tok", api_key_secret="sec")


@pytest.mark.asyncio
async def test_valid_standard_key_is_saved(monkeypatch):
    save = _wire(monkeypatch)
    assert await at.save_dialer_twilio_connection(_body(), _user=None) == {"ok": True}
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_key_secret_is_still_rejected(monkeypatch):
    save = _wire(monkeypatch, key_valid=False)
    with pytest.raises(HTTPException) as exc:
        await at.save_dialer_twilio_connection(_body(), _user=None)
    assert exc.value.status_code == 400
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_twiml_app_must_still_point_at_voice_connect(monkeypatch):
    save = _wire(monkeypatch, voice_url="https://example.com/somewhere-else")
    with pytest.raises(HTTPException) as exc:
        await at.save_dialer_twilio_connection(_body(), _user=None)
    assert exc.value.status_code == 422
    save.assert_not_awaited()
