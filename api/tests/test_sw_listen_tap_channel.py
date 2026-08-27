"""What /sw-listen subscribes to, and who is allowed to.

These exist because of a bug that could not be seen from either side alone. The
authorisation lookup matched a call by ``dialer_calls.id``; the Redis subscription
then used that same UUID as the channel name. But /sw-tap publishes under the
PROVIDER's call id -- ``parent_call_sid`` -- because that is the only id SWML has
when the tap is attached. Both halves passed their own reading of "correct" and the
result was a socket that authenticated, opened, and received nothing for ever.

So the load-bearing assertion here is not that a key comes back. It is that the key
is NOT the id that went in.
"""
import importlib.abc
import importlib.util
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _stub_pipecat_if_absent() -> None:
    """Let this module be imported without pipecat installed.

    Importing signalwire_routes pulls in api.services.telephony's package __init__,
    which registers EVERY provider -- including the ARI one, which imports pipecat.
    pipecat-ai does not build on macOS (numba/llvmlite), so without this the whole
    file is uncollectable on a developer's machine while passing in CI, which is the
    same as not having the test.

    Only installed when the real package is missing, so CI still exercises the real
    import chain. Nothing here is asserted on: the ARI transport is not on any path
    these tests touch.
    """
    if importlib.util.find_spec("pipecat") is not None:
        return

    class _Lazy(types.ModuleType):
        def __getattr__(self, name):  # noqa: D105 - any symbol, any submodule
            return MagicMock()

    class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "pipecat" or fullname.startswith("pipecat."):
                return importlib.util.spec_from_loader(fullname, self)
            return None

        def create_module(self, spec):
            return _Lazy(spec.name)

        def exec_module(self, module):
            module.__path__ = []

    sys.meta_path.insert(0, _Finder())


_stub_pipecat_if_absent()

from api.services.telephony.dialer.signalwire_routes import (
    _looks_like_uuid,
    _resolve_live_call,
    websocket_sw_listen,
)

_MODULE = "api.services.telephony.dialer.signalwire_routes"

REP = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
OUTBOUND_ROW_ID = "33333333-3333-4333-8333-333333333333"
INBOUND_ROW_ID = "44444444-4444-4444-8444-444444444444"

SW_CALL_ID = "b9f0c8de-provider-side-id"
INBOUND_CALL_ID = "in-7f3a"


def _rows(mapping: dict[str, list[dict]]):
    """Stand in for Supabase, answering per table."""
    async def fake(table: str, params: dict):
        return mapping.get(table, [])
    return fake


@pytest.mark.asyncio
async def test_outbound_resolves_to_the_provider_id_not_the_row_id():
    """The whole bug in one assertion."""
    with patch(
        f"{_MODULE}._supabase_rows",
        _rows({"dialer_calls": [{"parent_call_sid": SW_CALL_ID, "rep_user_id": REP}]}),
    ):
        key, own = await _resolve_live_call(OUTBOUND_ROW_ID, REP)

    assert key == SW_CALL_ID
    assert key != OUTBOUND_ROW_ID  # the regression itself
    assert own is True


@pytest.mark.asyncio
async def test_outbound_call_belonging_to_somebody_else_is_not_owned():
    with patch(
        f"{_MODULE}._supabase_rows",
        _rows({"dialer_calls": [{"parent_call_sid": SW_CALL_ID, "rep_user_id": OTHER}]}),
    ):
        key, own = await _resolve_live_call(OUTBOUND_ROW_ID, REP)

    # Still resolvable -- a manager may monitor it -- but not the caller's own call,
    # which is the only claim that unlocks the captions-only path.
    assert key == SW_CALL_ID
    assert own is False


@pytest.mark.asyncio
async def test_inbound_call_resolves_although_it_has_no_dialer_calls_row():
    """An answered inbound call is logged in inbound_calls and nowhere else.

    A rep on one had no id the copilot could ask about at all, which is why the panel
    read "Waiting for the call to connect" for the length of the conversation.
    """
    with patch(
        f"{_MODULE}._supabase_rows",
        _rows({
            "dialer_calls": [],
            "inbound_calls": [{
                "provider_call_id": INBOUND_CALL_ID,
                "answered_by": None,
                "target_user_ids": [REP, OTHER],
            }],
        }),
    ):
        key, own = await _resolve_live_call(INBOUND_ROW_ID, REP)

    assert key == INBOUND_CALL_ID
    assert own is True


@pytest.mark.asyncio
async def test_inbound_answered_by_counts_even_when_the_ring_list_does_not():
    with patch(
        f"{_MODULE}._supabase_rows",
        _rows({
            "dialer_calls": [],
            "inbound_calls": [{
                "provider_call_id": INBOUND_CALL_ID,
                "answered_by": REP,
                "target_user_ids": [],
            }],
        }),
    ):
        _, own = await _resolve_live_call(INBOUND_ROW_ID, REP)

    assert own is True


@pytest.mark.asyncio
async def test_inbound_call_a_rep_was_never_rung_for_is_not_theirs():
    with patch(
        f"{_MODULE}._supabase_rows",
        _rows({
            "dialer_calls": [],
            "inbound_calls": [{
                "provider_call_id": INBOUND_CALL_ID,
                "answered_by": OTHER,
                "target_user_ids": [OTHER],
            }],
        }),
    ):
        key, own = await _resolve_live_call(INBOUND_ROW_ID, REP)

    assert key == INBOUND_CALL_ID
    assert own is False


@pytest.mark.asyncio
async def test_a_non_uuid_is_taken_as_the_provider_id_and_is_never_owned():
    """It identifies no row and therefore no rep. Monitoring only."""
    called = False

    async def fake(table: str, params: dict):
        nonlocal called
        called = True
        return []

    with patch(f"{_MODULE}._supabase_rows", fake):
        key, own = await _resolve_live_call(SW_CALL_ID, REP)

    assert key == SW_CALL_ID
    assert own is False
    assert called is False  # and it never reaches Supabase with a value that would 400


@pytest.mark.asyncio
async def test_no_such_call_resolves_to_nothing():
    with patch(f"{_MODULE}._supabase_rows", _rows({})):
        assert await _resolve_live_call(OUTBOUND_ROW_ID, REP) == (None, False)


@pytest.mark.asyncio
async def test_a_read_that_fails_is_not_a_call():
    """Fails CLOSED. "We could not check" must never become "yes"."""
    async def boom(table: str, params: dict):
        return []  # _supabase_rows swallows and returns [] on every failure

    with patch(f"{_MODULE}._supabase_rows", boom):
        assert await _resolve_live_call(OUTBOUND_ROW_ID, REP) == (None, False)


@pytest.mark.asyncio
async def test_a_row_with_no_provider_id_yields_no_channel():
    """Half a row is not a call. Subscribing to "" would join a channel shared by
    every other half-written row."""
    with patch(
        f"{_MODULE}._supabase_rows",
        _rows({"dialer_calls": [{"parent_call_sid": "", "rep_user_id": REP}]}),
    ):
        key, _ = await _resolve_live_call(OUTBOUND_ROW_ID, REP)

    assert key is None


@pytest.mark.parametrize("value", [OUTBOUND_ROW_ID, REP])
def test_looks_like_uuid_accepts(value):
    assert _looks_like_uuid(value) is True


@pytest.mark.parametrize("value", ["", "in-7f3a", SW_CALL_ID, "../../etc", "eq.x"])
def test_looks_like_uuid_rejects(value):
    assert _looks_like_uuid(value) is False


# ── The wiring, not just the rule ────────────────────────────────────────────────
#
# The resolver being right proves nothing on its own: the bug was that the handler
# used a DIFFERENT value from the one authorisation had matched. So this asserts on
# what subscribe_stream is actually handed.


def _socket(params: dict) -> MagicMock:
    socket = MagicMock()
    socket.query_params = params
    socket.accept = AsyncMock()
    socket.close = AsyncMock()
    socket.send_text = AsyncMock()
    socket.send_bytes = AsyncMock()
    socket.receive = AsyncMock(return_value={"type": "websocket.disconnect"})
    return socket


@pytest.mark.asyncio
async def test_the_socket_subscribes_to_the_tap_channel_not_the_row_id():
    seen = {}

    async def fake_stream(call_id, stop):
        seen["call_id"] = call_id
        return
        yield  # pragma: no cover - makes this an async generator

    socket = _socket({
        "call_id": OUTBOUND_ROW_ID,
        "token": "t",
        "captions_only": "1",
    })

    with (
        patch(f"{_MODULE}._supabase_user_id", AsyncMock(return_value=REP)),
        patch(f"{_MODULE}._may_monitor_calls", AsyncMock(return_value=False)),
        patch(
            f"{_MODULE}._supabase_rows",
            _rows({"dialer_calls": [{"parent_call_sid": SW_CALL_ID, "rep_user_id": REP}]}),
        ),
        patch(f"{_MODULE}.subscribe_stream", fake_stream),
    ):
        await websocket_sw_listen(socket)

    socket.accept.assert_awaited_once()
    assert seen["call_id"] == SW_CALL_ID
    assert seen["call_id"] != OUTBOUND_ROW_ID


@pytest.mark.asyncio
async def test_a_call_that_resolves_to_nothing_is_refused_even_for_a_manager():
    """A manager role is permission to listen to a call, not permission to open a
    subscription to an arbitrary string."""
    socket = _socket({"call_id": OUTBOUND_ROW_ID, "token": "t"})

    with (
        patch(f"{_MODULE}._supabase_user_id", AsyncMock(return_value=REP)),
        patch(f"{_MODULE}._may_monitor_calls", AsyncMock(return_value=True)),
        patch(f"{_MODULE}._supabase_rows", _rows({})),
    ):
        await websocket_sw_listen(socket)

    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once()
    assert socket.close.await_args.kwargs["code"] == 4403


@pytest.mark.asyncio
async def test_a_rep_is_refused_their_colleagues_call():
    socket = _socket({"call_id": OUTBOUND_ROW_ID, "token": "t", "captions_only": "1"})

    with (
        patch(f"{_MODULE}._supabase_user_id", AsyncMock(return_value=REP)),
        patch(f"{_MODULE}._may_monitor_calls", AsyncMock(return_value=False)),
        patch(
            f"{_MODULE}._supabase_rows",
            _rows({"dialer_calls": [{"parent_call_sid": SW_CALL_ID, "rep_user_id": OTHER}]}),
        ),
    ):
        await websocket_sw_listen(socket)

    socket.accept.assert_not_awaited()
    assert socket.close.await_args.kwargs["code"] == 4403


@pytest.mark.asyncio
async def test_an_unauthenticated_socket_never_reaches_supabase():
    socket = _socket({"call_id": OUTBOUND_ROW_ID, "token": ""})

    reads = []

    async def spy(table: str, params: dict):
        reads.append(table)
        return []

    with (
        patch(f"{_MODULE}._supabase_user_id", AsyncMock(return_value=None)),
        patch(f"{_MODULE}._supabase_rows", spy),
    ):
        await websocket_sw_listen(socket)

    assert reads == []
    assert socket.close.await_args.kwargs["code"] == 4403


@pytest.mark.asyncio
async def test_a_missing_call_id_is_refused_before_anything_else():
    socket = _socket({"call_id": "", "token": "t"})

    with patch(f"{_MODULE}._supabase_user_id", AsyncMock(return_value=REP)) as auth:
        await websocket_sw_listen(socket)

    auth.assert_not_awaited()
    assert socket.close.await_args.kwargs["code"] == 4400
