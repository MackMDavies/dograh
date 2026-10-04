"""Focused checks for rep-scoped Telnyx WebRTC credential minting."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.services.telephony.dialer.telnyx_dialer import (
    TelnyxDialerProvider,
    TelnyxNotConfigured,
)


_MODULE = "api.services.telephony.dialer.telnyx_dialer"
_ROUTES = "api.services.telephony.providers.twilio.routes"


def _response(*, payload=None, text="", status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.text = text
    response.json = MagicMock(return_value=payload or {})
    response.raise_for_status = MagicMock()
    return response


def _http_client(*, gets=(), posts=()):
    client = AsyncMock()
    client.get = AsyncMock(side_effect=list(gets))
    client.post = AsyncMock(side_effect=list(posts))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_first_login_creates_and_persists_rep_specific_credential():
    db = MagicMock()
    db.get_platform_telnyx_dialer_credentials = AsyncMock(
        return_value={"api_key": "secret", "connection_id": "conn-1"}
    )
    db.get_platform_telnyx_user_credential = AsyncMock(return_value=None)
    db.save_platform_telnyx_user_credential = AsyncMock()
    client = _http_client(
        posts=[
            _response(payload={"data": {"id": "cred-rep-12"}}),
            _response(text="jwt-rep-12"),
        ]
    )

    with (
        patch(f"{_MODULE}.db_client", db),
        patch(f"{_MODULE}.httpx.AsyncClient", return_value=client),
    ):
        creds = await TelnyxDialerProvider().mint_credentials(user_id=12)

    assert creds.token == "jwt-rep-12"
    assert creds.identity == "rep-12"
    db.save_platform_telnyx_user_credential.assert_awaited_once_with(
        user_id=12,
        connection_id="conn-1",
        telephony_credential_id="cred-rep-12",
    )
    assert client.post.await_args_list[0].kwargs["json"] == {
        "connection_id": "conn-1",
        "name": "Sysevo Dialer Rep 12",
    }


async def test_repeat_login_reuses_rep_credential_without_creating_another():
    db = MagicMock()
    db.get_platform_telnyx_dialer_credentials = AsyncMock(
        return_value={"api_key": "secret", "connection_id": "conn-1"}
    )
    db.get_platform_telnyx_user_credential = AsyncMock(
        return_value={"connection_id": "conn-1", "telephony_credential_id": "cred-rep-12"}
    )
    db.save_platform_telnyx_user_credential = AsyncMock()
    client = _http_client(
        gets=[
            _response(
                payload={
                    "data": {
                        "resource_id": "connection:conn-1",
                        "expired": False,
                    }
                }
            )
        ],
        posts=[_response(text="jwt-rep-12")],
    )

    with (
        patch(f"{_MODULE}.db_client", db),
        patch(f"{_MODULE}.httpx.AsyncClient", return_value=client),
    ):
        creds = await TelnyxDialerProvider().mint_credentials(user_id=12)

    assert creds.token == "jwt-rep-12"
    assert client.post.await_count == 1
    db.save_platform_telnyx_user_credential.assert_not_awaited()


async def test_settings_database_failure_fails_closed():
    db = MagicMock()
    db.get_platform_telnyx_dialer_credentials = AsyncMock(side_effect=RuntimeError("db unavailable"))

    with patch(f"{_MODULE}.db_client", db):
        try:
            await TelnyxDialerProvider().mint_credentials(user_id=12)
        except TelnyxNotConfigured as exc:
            assert "no call was placed" in str(exc)
        else:
            raise AssertionError("A settings database failure must stop token issuance")


async def test_telnyx_call_start_returns_server_selected_rotating_caller_id():
    from uuid import UUID

    from api.services.telephony.providers.twilio.routes import (
        TelnyxDialerCallStartRequest,
        start_telnyx_dialer_call,
    )

    assigned = {
        "provider": "telnyx",
        "direction": "both",
        "phone_number": "+12095550123",
    }
    user = MagicMock(id=12, provider_id="supabase-user-1")
    body = TelnyxDialerCallStartRequest(
        call_id=UUID("d84d7a66-356d-4c29-9191-7c6834983784"),
        to_number="+12095550987",
    )

    with (
        patch(f"{_ROUTES}._get_assigned_dialer_number_or_503", new_callable=AsyncMock, return_value=assigned),
        patch(f"{_ROUTES}.create_dialer_call", new_callable=AsyncMock, return_value=True) as create,
    ):
        result = await start_telnyx_dialer_call(body, user)

    assert result["from_number"] == "+12095550123"
    assert create.await_args.kwargs["from_number"] == result["from_number"]


async def test_telnyx_call_start_rejects_inbound_only_line_without_creating_history():
    from uuid import UUID

    from api.services.telephony.providers.twilio.routes import (
        TelnyxDialerCallStartRequest,
        start_telnyx_dialer_call,
    )

    user = MagicMock(id=12, provider_id="supabase-user-1")
    body = TelnyxDialerCallStartRequest(
        call_id=UUID("d84d7a66-356d-4c29-9191-7c6834983784"),
        to_number="+12095550987",
    )
    with (
        patch(f"{_ROUTES}._get_assigned_dialer_number_or_503", new_callable=AsyncMock,
              return_value={"provider": "telnyx", "direction": "inbound", "phone_number": "+12095550123"}),
        patch(f"{_ROUTES}.create_dialer_call", new_callable=AsyncMock) as create,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await start_telnyx_dialer_call(body, user)

    assert exc_info.value.status_code == 409
    create.assert_not_awaited()


async def test_telnyx_inbound_answer_marks_the_owned_invite_answered():
    from api.services.telephony.providers.twilio.routes import (
        TelnyxInboundCallAnswerRequest,
        answer_telnyx_inbound_dialer_call,
    )

    response = _response(payload=[{"provider_call_id": "telnyx-call", "status": "answered"}])
    client = _http_client()
    client.patch = AsyncMock(return_value=response)
    user = MagicMock(provider_id="supabase-user-1")

    with (
        patch(f"{_ROUTES}.httpx.AsyncClient", return_value=client),
        patch(f"{_ROUTES}.SUPABASE_URL", "https://db.example"),
        patch(f"{_ROUTES}.SUPABASE_SERVICE_ROLE_KEY", "service-secret"),
    ):
        await answer_telnyx_inbound_dialer_call(
            TelnyxInboundCallAnswerRequest(call_id="telnyx-call"), user
        )

    kwargs = client.patch.await_args.kwargs
    assert kwargs["json"]["status"] == "answered"
    assert kwargs["json"]["answered_by"] == "supabase-user-1"
    assert kwargs["params"]["target_user_ids"] == "cs.{supabase-user-1}"
    assert kwargs["params"]["status"] == "eq.ringing"


async def test_telnyx_inbound_answer_rejects_a_call_claimed_by_another_rep():
    from api.services.telephony.providers.twilio.routes import (
        TelnyxInboundCallAnswerRequest,
        answer_telnyx_inbound_dialer_call,
    )

    client = _http_client()
    client.patch = AsyncMock(return_value=_response(payload=[]))
    client.get = AsyncMock(return_value=_response(payload=[{
        "status": "answered", "answered_by": "different-user",
    }]))
    user = MagicMock(provider_id="supabase-user-1")

    with (
        patch(f"{_ROUTES}.httpx.AsyncClient", return_value=client),
        patch(f"{_ROUTES}.SUPABASE_URL", "https://db.example"),
        patch(f"{_ROUTES}.SUPABASE_SERVICE_ROLE_KEY", "service-secret"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await answer_telnyx_inbound_dialer_call(
                TelnyxInboundCallAnswerRequest(call_id="telnyx-call"), user
            )

    assert exc_info.value.status_code == 409


async def test_telnyx_inbound_close_recovers_answer_if_answer_callback_races_teardown():
    from api.services.telephony.providers.twilio.routes import (
        TelnyxInboundCallEndRequest,
        end_telnyx_inbound_dialer_call,
    )

    client = _http_client()
    client.patch = AsyncMock(side_effect=[_response(payload=[]), _response(payload=[])])
    user = MagicMock(provider_id="supabase-user-1")

    with (
        patch(f"{_ROUTES}.httpx.AsyncClient", return_value=client),
        patch(f"{_ROUTES}.SUPABASE_URL", "https://db.example"),
        patch(f"{_ROUTES}.SUPABASE_SERVICE_ROLE_KEY", "service-secret"),
        patch(f"{_ROUTES}.update_dialer_call_status", new_callable=AsyncMock),
    ):
        await end_telnyx_inbound_dialer_call(
            TelnyxInboundCallEndRequest(
                call_id="telnyx-call", outcome="completed", duration_seconds=1,
            ),
            user,
        )

    # Teardown never claims the call as answered. It only marks an unanswered
    # ringing invite missed, or closes a call already claimed by the answer route.
    first_patch = client.patch.await_args_list[0].kwargs
    second_patch = client.patch.await_args_list[1].kwargs
    assert first_patch["params"]["status"] == "eq.ringing"
    assert first_patch["json"]["status"] == "missed"
    assert second_patch["params"]["status"] == "eq.answered"
    assert second_patch["json"]["status"] == "answered"
