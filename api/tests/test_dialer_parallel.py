"""Tests for durable parallel-session state and atomic winner delegation."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.providers.twilio import dialer_parallel


def _configure(monkeypatch):
    monkeypatch.setattr(dialer_parallel, "SUPABASE_URL", "https://db.example")
    monkeypatch.setattr(dialer_parallel, "SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr(dialer_parallel, "SUPABASE_ANON_KEY", "anon-key")


def _client(response):
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.parametrize("operation", ["start_parallel_session", "claim_parallel_winner"])
async def test_parallel_rpc_returns_database_claim(monkeypatch, operation):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=True)
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        if operation == "start_parallel_session":
            result = await dialer_parallel.start_parallel_session(
                parent_call_sid="CA" + "1" * 32,
                rep_user_id="user-1",
                entry_ids=["entry-1", "entry-2"],
                line_count=2,
            )
        else:
            result = await dialer_parallel.claim_parallel_winner(
                parent_call_sid="CA" + "1" * 32,
                entry_id="entry-1",
                child_call_sid="CA" + "2" * 32,
                leg_id="leg-1",
            )

    assert result is True
    assert client.post.await_count == 1
    assert client.post.await_args.args[0].startswith("https://db.example/rest/v1/rpc/")
    if operation == "start_parallel_session":
        assert client.post.await_args.args[0].endswith("/dialer_start_parallel_session_v2")


async def test_register_parallel_leg_writes_provider_child_identity(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=True)
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        assert await dialer_parallel.register_parallel_leg(
            leg_id="leg-1",
            child_call_sid="CA" + "2" * 32,
        )

    assert client.post.await_args.kwargs["json"] == {
        "p_leg_id": "leg-1",
        "p_child_call_sid": "CA" + "2" * 32,
    }


async def test_record_parallel_timing_uses_service_role_rpc(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=True)
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        result = await dialer_parallel.record_parallel_timing(
            parent_call_sid="CA" + "1" * 32,
            child_call_sid="CA" + "2" * 32,
            event_kind="answer",
            event_at="2026-10-18T12:34:56+00:00",
            leg_id="leg-1",
        )

    assert result is True
    assert client.post.await_args.args[0].endswith("/rpc/dialer_parallel_record_timing")
    assert client.post.await_args.kwargs["json"] == {
        "p_parent_call_sid": "CA" + "1" * 32,
        "p_child_call_sid": "CA" + "2" * 32,
        "p_event_kind": "answer",
        "p_event_at": "2026-10-18T12:34:56+00:00",
        "p_leg_id": "leg-1",
    }


async def test_record_parallel_timing_rejects_unknown_event_before_rpc(monkeypatch):
    _configure(monkeypatch)
    with patch.object(dialer_parallel.httpx, "AsyncClient") as client:
        assert not await dialer_parallel.record_parallel_timing(
            parent_call_sid="CA" + "1" * 32,
            child_call_sid="CA" + "2" * 32,
            event_kind="invented",
            event_at="2026-10-18T12:34:56+00:00",
        )
    client.assert_not_called()


async def test_record_parallel_timing_evaluates_guard_after_join(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=True)
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client), patch.object(
        dialer_parallel, "apply_parallel_abandon_guard", new_callable=AsyncMock
    ) as apply_guard:
        result = await dialer_parallel.record_parallel_timing(
            parent_call_sid="CA" + "1" * 32,
            child_call_sid="CA" + "2" * 32,
            event_kind="lead_join",
            event_at="2026-10-18T12:34:56+00:00",
        )

    assert result is True
    apply_guard.assert_awaited_once_with(parent_call_sid="CA" + "1" * 32)


async def test_finish_parallel_session_evaluates_guard_after_success(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=True)
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client), patch.object(
        dialer_parallel, "apply_parallel_abandon_guard", new_callable=AsyncMock
    ) as apply_guard:
        result = await dialer_parallel.finish_parallel_session(
            parent_call_sid="CA" + "1" * 32,
            status="completed",
        )

    assert result is True
    apply_guard.assert_awaited_once_with(parent_call_sid="CA" + "1" * 32)


async def test_second_human_route_requires_atomic_inbound_reservation(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[{
        "inbound_call_id": "inbound-1",
        "rep_user_id": "rep-2",
        "caller_name": "Example Co",
    }])
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        result = await dialer_parallel.route_parallel_to_teammate(
            parent_call_sid="CA" + "1" * 32,
            child_call_sid="CA" + "2" * 32,
            conference_name="call-CA" + "2" * 32,
            entry_id="entry-1",
            leg_id="leg-1",
        )

    assert result == {
        "inbound_call_id": "inbound-1",
        "rep_user_id": "rep-2",
        "caller_name": "Example Co",
    }
    assert client.post.await_args.args[0].endswith("/dialer_parallel_route_to_teammate")
    assert client.post.await_args.kwargs["json"] == {
        "p_parent_call_sid": "CA" + "1" * 32,
        "p_child_call_sid": "CA" + "2" * 32,
        "p_conference_name": "call-CA" + "2" * 32,
        "p_entry_id": "entry-1",
        "p_leg_id": "leg-1",
    }


async def test_assign_parallel_caller_id_persists_before_originating(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=True)
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        assert await dialer_parallel.assign_parallel_caller_id(
            parent_call_sid="CA" + "1" * 32,
            leg_id="leg-1",
            rep_user_id="user-1",
            caller_id="+15551234567",
        )

    assert client.post.await_args.args[0].endswith("/rpc/dialer_parallel_assign_caller_id")
    assert client.post.await_args.kwargs["json"] == {
        "p_parent_call_sid": "CA" + "1" * 32,
        "p_leg_id": "leg-1",
        "p_rep_user_id": "user-1",
        "p_caller_id": "+15551234567",
    }


async def test_parallel_targets_are_loaded_from_server_validated_session(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[
        {"leg_id": "leg-1", "entry_id": "entry-1", "phone": "+15550000001", "business": "One"},
        {"leg_id": "leg-2", "entry_id": "entry-2", "phone": "+15550000002", "business": "Two"},
        {"leg_id": None, "entry_id": "entry-3", "phone": "+15550000003", "business": "Invalid"},
    ])
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        rows = await dialer_parallel.parallel_targets(
            parent_call_sid="CA" + "1" * 32,
            rep_user_id="user-1",
        )

    assert [row["entry_id"] for row in rows] == ["entry-1", "entry-2"]
    assert client.post.await_args.args[0].endswith("/rpc/dialer_parallel_targets")


async def test_candidate_phones_returns_exact_server_rows_for_preflight(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[
        {"id": "entry-1", "phone": "+15550000001"},
        {"id": "entry-2", "phone": "+15550000002"},
    ])
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        phones = await dialer_parallel.candidate_phones(["entry-1", "entry-2"])

    assert phones == {"entry-1": "+15550000001", "entry-2": "+15550000002"}
    assert client.get.await_args.args[0].endswith("/cold_call_entries")


async def test_candidate_phones_fails_closed_when_any_entry_is_missing(monkeypatch):
    _configure(monkeypatch)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[{"id": "entry-1", "phone": "+15550000001"}])
    client = _client(response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        phones = await dialer_parallel.candidate_phones(["entry-1", "entry-2"])

    assert phones == {}


async def test_parallel_session_fails_closed_without_service_credentials(monkeypatch):
    monkeypatch.setattr(dialer_parallel, "SUPABASE_URL", "")
    monkeypatch.setattr(dialer_parallel, "SUPABASE_SERVICE_ROLE_KEY", "")
    with patch.object(dialer_parallel.httpx, "AsyncClient") as client:
        assert not await dialer_parallel.start_parallel_session(
            parent_call_sid="CA" + "1" * 32,
            rep_user_id="user-1",
            entry_ids=["entry-1", "entry-2"],
            line_count=2,
        )
    client.assert_not_called()


async def test_available_teammates_uses_account_scoped_rpc(monkeypatch):
    _configure(monkeypatch)
    teammates_response = MagicMock()
    teammates_response.raise_for_status = MagicMock()
    teammates_response.json = MagicMock(return_value=[
        {"user_id": "rep-2", "full_name": "Rep Two", "reach": "live"},
        {"user_id": "rep-3", "full_name": "Rep Three", "reach": "push"},
        {"user_id": "bad", "full_name": "Bad reach", "reach": "offline"},
        {"user_id": "malformed"},
    ])
    client = _client(teammates_response)

    with patch.object(dialer_parallel.httpx, "AsyncClient", return_value=client):
        users = await dialer_parallel.available_teammates(rep_user_id="rep-1")

    assert users == [
        {"user_id": "rep-2", "full_name": "Rep Two", "reach": "live"},
        {"user_id": "rep-3", "full_name": "Rep Three", "reach": "push"},
    ]
    assert client.post.await_args.args[0].endswith("/rpc/dialer_available_teammates")
    assert client.post.await_args.kwargs["json"] == {"p_rep_user_id": "rep-1"}
