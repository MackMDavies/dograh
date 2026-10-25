"""Warm transfers on Asterisk (ARI) lines (phone tools phase 5)."""

from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony import ari_manager as ari
from api.services.telephony.transfer_event_protocol import TransferEventType
from api.tests.test_warm_transfer import FakeTransfers, _context


def _connection(store, play_result=None):
    conn = ari.ARIConnection.__new__(ari.ARIConnection)
    conn.organization_id = 1
    conn._get_transfer_manager = AsyncMock(return_value=store)
    conn._ari_request = AsyncMock(return_value={"id": "pb-1"} if play_result is None else play_result)
    conn._delete_channel = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_the_colleague_hears_the_introduction_and_1_connects():
    store = FakeTransfers(_context())
    conn = _connection(store)
    with patch("api.utils.common.get_backend_endpoints", AsyncMock(return_value=("https://api.x", "wss://api.x"))), \
         patch.object(ari.asyncio, "create_task", lambda coro: coro.close()):
        await conn._handle_destination_answered("t1", "chan-1")
    method, path = conn._ari_request.await_args_list[0].args
    assert (method, path) == ("POST", "/channels/chan-1/play")
    assert conn._ari_request.await_args_list[0].kwargs["params"]["media"] == "sound:https://api.x/api/v1/telephony/ari/transfer-intro/t1.wav"
    assert store.events == [] and store.contexts["t1"].answered

    await conn._warm_transfer_key("t1", "chan-1", "1")
    await conn._warm_transfer_key("t1", "chan-1", "1")  # reported once
    assert [e.type for e in store.events] == [TransferEventType.DESTINATION_ANSWERED]
    conn._delete_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_key_or_silence_declines_and_hangs_up_the_colleague():
    for digit in ("7", ""):
        store = FakeTransfers(_context(answered=True))
        conn = _connection(store)
        await conn._warm_transfer_key("t1", "chan-1", digit)
        assert [(e.type, e.reason) for e in store.events] == [(TransferEventType.TRANSFER_FAILED, "declined")]
        conn._delete_channel.assert_awaited_once_with("chan-1")


@pytest.mark.asyncio
async def test_hanging_up_during_the_introduction_is_a_decline_reported_once():
    store = FakeTransfers(_context(answered=True))
    conn = _connection(store)
    await conn._handle_transfer_failed("t1", "chan-1", "Normal Clearing")
    await conn._handle_transfer_failed("t1", "chan-1", "Normal Clearing")
    assert [e.reason for e in store.events] == ["declined"]


@pytest.mark.asyncio
async def test_if_the_introduction_cannot_play_the_caller_is_connected_cold():
    store = FakeTransfers(_context())
    conn = _connection(store, play_result={})
    with patch("api.utils.common.get_backend_endpoints", AsyncMock(return_value=("https://api.x", "wss://api.x"))):
        await conn._handle_destination_answered("t1", "chan-1")
    assert [e.type for e in store.events] == [TransferEventType.DESTINATION_ANSWERED]


@pytest.mark.asyncio
async def test_no_introduction_audio_means_a_cold_transfer():
    from api.services.telephony.providers.ari.provider import ARIProvider

    store = FakeTransfers(_context())
    provider = ARIProvider.__new__(ARIProvider)
    provider.validate_config = lambda: True
    with patch("api.services.telephony.warm_transfer.prepare_ari_introduction", AsyncMock(return_value=False)), \
         patch("api.services.telephony.call_transfer_manager.get_call_transfer_manager", AsyncMock(return_value=store)):
        stored = []
        original_store = store.store_transfer_context

        async def remember(context):
            stored.append(context)
            await original_store(context)

        store.store_transfer_context = remember
        try:
            await provider.transfer_call("PJSIP/100", "t1", "conf", introduction="Jane")
        except Exception:
            pass  # the channel originate itself is not under test
    assert stored and stored[0].warm is False and stored[0].introduction is None
