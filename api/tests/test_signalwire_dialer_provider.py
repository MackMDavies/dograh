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
    # ONE shared subscriber for the whole dialer - see the provider comment.
    # A per-rep reference would bill per rep; a random one would create a new
    # every page load. This assertion is the guard against that regressing.
    assert kwargs["json"] == {"reference": "sysevo-dialer"}
    assert kwargs["auth"] == ("proj-123", "PTtoken")


async def test_mint_credentials_uses_same_reference_for_repeat_calls(monkeypatch):
    _configure(monkeypatch)
    client = _fake_client(_ok_response({"token": "t"}))

    with patch(f"{_MODULE}.httpx.AsyncClient", return_value=client):
        await SignalWireDialerProvider().mint_credentials(user_id=7)
        await SignalWireDialerProvider().mint_credentials(user_id=7)

    references = [c.kwargs["json"]["reference"] for c in client.post.call_args_list]
    assert references == ["sysevo-dialer", "sysevo-dialer"]


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


# --- Error decoding -------------------------------------------------------
# These pin the real payload SignalWire returned in production on
# 2026-08-14, when the account ran out of balance and the only thing logged
# was "422 unknown".


def _resp(status, json_body=None, text=""):
    import httpx

    if json_body is not None:
        return httpx.Response(status, json=json_body)
    return httpx.Response(status, text=text)


def test_describe_names_insufficient_balance_as_an_account_action():
    from api.services.telephony.dialer.signalwire_dialer import (
        _describe_signalwire_error,
    )

    detail = _describe_signalwire_error(
        _resp(
            422,
            {
                "errors": [
                    {
                        "type": "account_error",
                        "code": "insufficient_balance",
                        "message": "The account has insufficient balance",
                    }
                ]
            },
        )
    )
    assert "insufficient balance" in detail.lower()
    assert "top it up" in detail.lower()
    assert "422" not in detail


def test_describe_falls_back_to_message_and_code_for_unknown_errors():
    from api.services.telephony.dialer.signalwire_dialer import (
        _describe_signalwire_error,
    )

    detail = _describe_signalwire_error(
        _resp(400, {"errors": [{"code": "bad_reference", "message": "Nope"}]})
    )
    assert detail == "Nope (bad_reference)"


def test_describe_survives_a_non_json_body():
    from api.services.telephony.dialer.signalwire_dialer import (
        _describe_signalwire_error,
    )

    detail = _describe_signalwire_error(_resp(502, text="<html>bad gateway</html>"))
    assert "502" in detail


def test_describe_bounds_a_hostile_body():
    from api.services.telephony.dialer.signalwire_dialer import (
        _describe_signalwire_error,
    )

    detail = _describe_signalwire_error(
        _resp(422, {"errors": [{"code": "x", "message": "A" * 5000}]})
    )
    assert len(detail) < 400


def test_describe_handles_an_empty_or_odd_error_list():
    from api.services.telephony.dialer.signalwire_dialer import (
        _describe_signalwire_error,
    )

    assert "422" in _describe_signalwire_error(_resp(422, {"errors": []}))
    assert "422" in _describe_signalwire_error(_resp(422, {"nope": 1}))


async def test_mint_surfaces_the_balance_reason_to_the_caller(monkeypatch):
    """The rep must see WHY, not 'refused'. /voice-token turns this text
    into the 503 detail the dialer banner renders."""
    import httpx
    import pytest as _pytest
    from unittest.mock import AsyncMock, patch

    from api.services.telephony.dialer.signalwire_dialer import (
        SignalWireDialerProvider,
        SignalWireNotConfigured,
    )

    monkeypatch.setenv("SIGNALWIRE_SPACE_URL", "example.signalwire.com")
    monkeypatch.setenv("SIGNALWIRE_PROJECT_ID", "proj")
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "tok")

    resp = httpx.Response(
        422,
        json={
            "errors": [
                {"code": "insufficient_balance", "message": "The account has insufficient balance"}
            ]
        },
        request=httpx.Request("POST", "https://example.signalwire.com/x"),
    )
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "api.services.telephony.dialer.signalwire_dialer.httpx.AsyncClient",
        return_value=client,
    ):
        with _pytest.raises(SignalWireNotConfigured) as ei:
            await SignalWireDialerProvider().mint_credentials(user_id=1)

    assert "insufficient balance" in str(ei.value).lower()


# --- Cost containment -----------------------------------------------------
# SignalWire bills each subscriber monthly, and `reference` is create-or-get,
# so a per-rep reference would provision a billable resource per rep. These
# pin the rule: the ONLY things that may cost money are a dial and a phone
# number. Subscriber count must not scale with headcount.


def _sw_env(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_SPACE_URL", "example.signalwire.com")
    monkeypatch.setenv("SIGNALWIRE_PROJECT_ID", "proj")
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "tok")
    monkeypatch.delenv("SIGNALWIRE_SUBSCRIBER_REFERENCE", raising=False)


def _capturing_client(refs):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"token": "t"}

    async def _post(url, **kw):
        refs.append(kw["json"]["reference"])
        return resp

    c = AsyncMock()
    c.post = AsyncMock(side_effect=_post)
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    return c


async def test_every_rep_shares_one_billable_subscriber(monkeypatch):
    from unittest.mock import patch

    from api.services.telephony.dialer.signalwire_dialer import (
        SignalWireDialerProvider,
    )

    _sw_env(monkeypatch)
    refs: list[str] = []
    with patch(
        "api.services.telephony.dialer.signalwire_dialer.httpx.AsyncClient",
        return_value=_capturing_client(refs),
    ):
        p = SignalWireDialerProvider()
        for uid in (1, 42, 7, 999, 12345):
            await p.mint_credentials(user_id=uid)

    # Five different reps, ONE subscriber reference -> one billable resource.
    assert len(set(refs)) == 1, f"headcount would create {len(set(refs))} subscribers"
    assert refs[0] == "sysevo-dialer"


async def test_rep_identity_is_still_per_rep_for_attribution(monkeypatch):
    from unittest.mock import patch

    from api.services.telephony.dialer.signalwire_dialer import (
        SignalWireDialerProvider,
    )

    _sw_env(monkeypatch)
    refs: list[str] = []
    with patch(
        "api.services.telephony.dialer.signalwire_dialer.httpx.AsyncClient",
        return_value=_capturing_client(refs),
    ):
        p = SignalWireDialerProvider()
        a = await p.mint_credentials(user_id=1)
        b = await p.mint_credentials(user_id=2)

    # Sharing the SUBSCRIBER must not collapse per-rep attribution.
    assert a.identity == "rep-1"
    assert b.identity == "rep-2"


async def test_subscriber_reference_is_never_randomised(monkeypatch):
    """A random reference silently creates a NEW billable subscriber on every
    call - this is exactly how three junk subscribers got provisioned."""
    from unittest.mock import patch

    from api.services.telephony.dialer.signalwire_dialer import (
        SignalWireDialerProvider,
    )

    _sw_env(monkeypatch)
    refs: list[str] = []
    with patch(
        "api.services.telephony.dialer.signalwire_dialer.httpx.AsyncClient",
        return_value=_capturing_client(refs),
    ):
        p = SignalWireDialerProvider()
        for _ in range(4):
            await p.mint_credentials(user_id=1)

    assert len(set(refs)) == 1
