"""Transfers on Cloudonix (phone tools phase 4)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.providers.cloudonix import routes
from api.services.telephony.transfer_event_protocol import TransferEventType
from api.tests.test_warm_transfer import FakeTransfers, _context

BACKEND = ("https://api.x", "wss://api.x")


class Form:
    def __init__(self, data): self._d = data
    async def form(self): return self._d


def _patched(store):
    return patch("api.services.telephony.call_transfer_manager.get_call_transfer_manager", AsyncMock(return_value=store))


@pytest.mark.asyncio
async def test_warm_answer_reads_the_introduction_then_1_connects():
    store = FakeTransfers(_context())
    with _patched(store), patch("api.utils.common.get_backend_endpoints", AsyncMock(return_value=BACKEND)):
        answer = (await routes.cloudonix_transfer_answer("t1")).body.decode()
        assert "<Gather" in answer and "/cloudonix/transfer-accept/t1" in answer and "<Conference" not in answer
        accept = (await routes.cloudonix_transfer_accept("t1", Form({"Digits": "1"}))).body.decode()
    assert "transfer-CA-caller</Conference>" in accept
    assert [e.type for e in store.events] == [TransferEventType.DESTINATION_ANSWERED]


@pytest.mark.asyncio
async def test_the_caller_joins_after_the_stream_only_when_the_transfer_connected():
    store = FakeTransfers(_context(original_call_sid="sess-1", accepted=True, resolved=True))
    store.find_transfer_context_for_call = AsyncMock(return_value=store.contexts["t1"])
    run = SimpleNamespace(gathered_context={"call_id": "sess-1"})
    with _patched(store), patch.object(__import__("api.db", fromlist=["db_client"]).db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)):
        joined = (await routes.cloudonix_after_stream(9)).body.decode()
    assert "transfer-CA-caller</Conference>" in joined

    store.find_transfer_context_for_call = AsyncMock(return_value=None)
    with _patched(store), patch.object(__import__("api.db", fromlist=["db_client"]).db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)):
        ended = (await routes.cloudonix_after_stream(9)).body.decode()
    assert "<Hangup/>" in ended and "<Conference" not in ended


def test_every_cloudonix_call_returns_to_dograh_after_its_stream():
    import inspect
    from api.services.telephony.providers.cloudonix.provider import CloudonixProvider

    for method in (CloudonixProvider.initiate_call, CloudonixProvider.start_inbound_stream):
        assert "/api/v1/telephony/cloudonix/after-stream/" in inspect.getsource(method)
    provider = CloudonixProvider.__new__(CloudonixProvider)
    assert provider.supports_transfers() and provider.supports_warm_transfer()
