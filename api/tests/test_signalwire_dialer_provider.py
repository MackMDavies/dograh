"""Unit tests for the SignalWire dialer provider's token minting."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.services.telephony.dialer.signalwire_dialer import (
    SignalWireDialerProvider,
    SignalWireNotConfigured,
    _space_host,
    resolve_dialer_destination,
)

_MODULE = "api.services.telephony.dialer.signalwire_dialer"


def _configure(monkeypatch, **overrides) -> None:
    env = {
        "SIGNALWIRE_SPACE_URL": "sysevo.signalwire.com",
        "SIGNALWIRE_PROJECT_ID": "proj-123",
        "SIGNALWIRE_API_TOKEN": "PTtoken",
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _fake_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=payload)
    return response


async def test_mint_credentials_posts_stable_reference_and_returns_token(monkeypatch):
    _configure(monkeypatch)
    client = _fake_client(
        _ok_response({"subscriber_id": "sub-1", "token": "eyJhbGciOi"})
    )

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        creds = await SignalWireDialerProvider().mint_credentials(user_id=42)

    assert creds.token == "eyJhbGciOi"
    assert creds.identity == "rep-42"
    assert creds.destination == "/public/sysevo-dialer?channel=audio"

    _, kwargs = client.post.call_args
    args, _ = client.post.call_args
    assert args[0] == "https://sysevo.signalwire.com/api/fabric/subscribers/tokens"
    # Stable per rep - a random reference would create a new subscriber on
    # every page load. This assertion is the guard against that regressing.
    assert kwargs["json"] == {"reference": "rep-42"}
    assert kwargs["auth"] == ("proj-123", "PTtoken")


async def test_mint_credentials_uses_same_reference_for_repeat_calls(monkeypatch):
    _configure(monkeypatch)
    client = _fake_client(_ok_response({"token": "t"}))

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        await SignalWireDialerProvider().mint_credentials(user_id=7)
        await SignalWireDialerProvider().mint_credentials(user_id=7)

    references = [c.kwargs["json"]["reference"] for c in client.post.call_args_list]
    assert references == ["rep-7", "rep-7"]


async def test_mint_credentials_honours_destination_override(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("SIGNALWIRE_DIALER_DESTINATION", "/public/other?channel=audio")
    client = _fake_client(_ok_response({"token": "t"}))

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        creds = await SignalWireDialerProvider().mint_credentials(user_id=1)

    assert creds.destination == "/public/other?channel=audio"


@pytest.mark.parametrize(
    "missing",
    ["SIGNALWIRE_SPACE_URL", "SIGNALWIRE_PROJECT_ID", "SIGNALWIRE_API_TOKEN"],
)
async def test_mint_credentials_raises_when_env_missing(monkeypatch, missing):
    _configure(monkeypatch, **{missing: None})

    with pytest.raises(SignalWireNotConfigured) as exc:
        await SignalWireDialerProvider().mint_credentials(user_id=1)

    assert missing in str(exc.value)


async def test_mint_credentials_raises_on_http_error(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock())
    )
    client = _fake_client(response)

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        with pytest.raises(SignalWireNotConfigured):
            await SignalWireDialerProvider().mint_credentials(user_id=1)


async def test_mint_credentials_raises_on_transport_error(monkeypatch):
    _configure(monkeypatch)
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        with pytest.raises(SignalWireNotConfigured):
            await SignalWireDialerProvider().mint_credentials(user_id=1)


@pytest.mark.parametrize("payload", [{}, {"subscriber_id": "s"}, {"token": ""}, {"token": 5}, []])
async def test_mint_credentials_raises_when_response_has_no_token(monkeypatch, payload):
    _configure(monkeypatch)
    client = _fake_client(_ok_response(payload))

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        with pytest.raises(SignalWireNotConfigured):
            await SignalWireDialerProvider().mint_credentials(user_id=1)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sysevo.signalwire.com", "sysevo.signalwire.com"),
        ("https://sysevo.signalwire.com", "sysevo.signalwire.com"),
        ("https://sysevo.signalwire.com/", "sysevo.signalwire.com"),
        ("  http://sysevo.signalwire.com/ ", "sysevo.signalwire.com"),
        ("", ""),
    ],
)
def test_space_host_normalisation(monkeypatch, raw, expected):
    monkeypatch.setenv("SIGNALWIRE_SPACE_URL", raw)
    assert _space_host() == expected


def test_resolve_dialer_destination_defaults(monkeypatch):
    monkeypatch.delenv("SIGNALWIRE_DIALER_DESTINATION", raising=False)
    assert resolve_dialer_destination() == "/public/sysevo-dialer?channel=audio"


async def test_provider_is_reachable_through_the_abstraction(monkeypatch):
    """get_dialer_provider('signalwire') must hand back the real thing, not
    the placeholder that used to raise NotImplementedError."""
    from api.services.telephony.dialer.provider import get_dialer_provider

    provider = get_dialer_provider("signalwire")
    assert isinstance(provider, SignalWireDialerProvider)

    _configure(monkeypatch)
    client = _fake_client(_ok_response({"token": "tok"}))
    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        creds = await provider.mint_credentials(user_id=3)
    assert creds.token == "tok"
