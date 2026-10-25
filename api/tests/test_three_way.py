"""Three-way calls: the colleague joins the caller and the agent (phone tools phase 5)."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.services.telephony import ari_manager as ari
from api.services.telephony.transfer_event_protocol import TransferEvent, TransferEventType
from api.tests.test_warm_transfer import FakeTransfers, _context, _provider, _tool, _transfer_manager


def _joined(transfer_id):
    return TransferEvent(type=TransferEventType.DESTINATION_ANSWERED, transfer_id=transfer_id,
                         original_call_sid="CA-caller", status="success", action="colleague_joined")


@pytest.mark.asyncio
async def test_the_agent_stays_on_when_the_colleague_joins():
    store = FakeTransfers()
    store.outcomes = [_joined]
    provider = _provider()
    provider.supports_three_way = lambda: True
    manager, engine, patches = _transfer_manager(provider, store)
    handler = manager._create_transfer_call_handler(_tool(transfer_mode="three_way"), "add_colleague")
    result = AsyncMock()
    for p in patches: p.start()
    try:
        await handler(SimpleNamespace(arguments={}, result_callback=result))
    finally:
        for p in patches: p.stop()
    assert result.await_args.args[0]["status"] == "colleague_joined"
    engine.end_call_with_reason.assert_not_awaited()
    assert list(store.contexts.values())[0].three_way is True
    assert "introduction" not in provider.transfer_call.await_args.kwargs
    assert engine.log_phone_tool_event.await_args.args[0] == "conference"


@pytest.mark.asyncio
async def test_carriers_without_three_way_get_a_warm_transfer():
    store = FakeTransfers()
    store.outcomes = [lambda tid: TransferEvent(type=TransferEventType.DESTINATION_ANSWERED, transfer_id=tid,
                                                 original_call_sid="CA-caller", status="success", action="destination_answered")]
    provider = _provider()
    provider.supports_three_way = lambda: False
    manager, engine, patches = _transfer_manager(provider, store)
    handler = manager._create_transfer_call_handler(_tool(transfer_mode="three_way"), "add_colleague")
    for p in patches: p.start()
    try:
        await handler(SimpleNamespace(arguments={"introduction": "Jane"}, result_callback=AsyncMock()))
    finally:
        for p in patches: p.stop()
    assert provider.transfer_call.await_args.kwargs["introduction"] == "Jane"
    engine.end_call_with_reason.assert_awaited_once()


@pytest.mark.asyncio
async def test_asterisk_adds_the_colleague_to_the_callers_bridge():
    store = FakeTransfers(_context(three_way=True, warm=False, introduction=None))
    conn = ari.ARIConnection.__new__(ari.ARIConnection)
    conn.organization_id = 1
    conn._get_transfer_manager = AsyncMock(return_value=store)
    conn._delete_channel = AsyncMock()

    async def request(method, path, **kwargs):
        return {"channels": ["caller", "ext", "chan-9"]} if method == "GET" else {}

    conn._ari_request = AsyncMock(side_effect=request)
    run = SimpleNamespace(gathered_context={"bridge_id": "br-1"})
    with patch.object(ari.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch.object(ari.db_client, "update_workflow_run", AsyncMock()) as update:
        await conn._handle_destination_answered("t1", "chan-9")
    assert conn._ari_request.await_args_list[0].args == ("POST", "/bridges/br-1/addChannel")
    assert store.events[0].type == TransferEventType.DESTINATION_ANSWERED
    assert update.await_args.kwargs["gathered_context"] == {"three_way_channels": ["chan-9"]}

    await conn._handle_transfer_failed("t1", "chan-9", "Normal Clearing")
    assert len(store.events) == 1  # the colleague leaving later is not a failed transfer


def test_the_colleague_is_hung_up_with_the_call():
    assert "three_way_channels" in inspect.getsource(ari.ARIConnection._handle_stasis_end)


def test_the_tools_api_accepts_three_way():
    from api.routes.tool import TransferCallConfig

    assert TransferCallConfig(destination="PJSIP/100", transfer_mode="three_way").transfer_mode == "three_way"
