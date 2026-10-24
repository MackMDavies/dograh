"""Warm transfers with a backup number (Keypad & Native Phone Tools, phase 2)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.services.telephony import warm_transfer
from api.services.telephony.transfer_event_protocol import (
    TransferContext,
    TransferEvent,
    TransferEventType,
)


class FakeTransfers:
    """In-memory stand-in for CallTransferManager."""

    def __init__(self, *contexts: TransferContext):
        self.contexts = {c.transfer_id: c for c in contexts}
        self.events: list[TransferEvent] = []
        self.outcomes: list = []  # what wait_for_transfer_completion returns, in order

    async def get_transfer_context(self, transfer_id):
        context = self.contexts.get(transfer_id)
        return TransferContext.from_json(context.to_json()) if context else None

    async def store_transfer_context(self, context):
        self.contexts[context.transfer_id] = TransferContext.from_json(context.to_json())

    async def remove_transfer_context(self, transfer_id):
        self.contexts.pop(transfer_id, None)

    async def publish_transfer_event(self, event):
        self.events.append(event)

    async def wait_for_transfer_completion(self, transfer_id, timeout):
        outcome = self.outcomes.pop(0)
        return outcome(transfer_id) if callable(outcome) else outcome


def _context(**kw) -> TransferContext:
    base = dict(
        transfer_id="t1", call_sid="CA-dest", target_number="+15550001111", tool_uuid="tool",
        original_call_sid="CA-caller", conference_name="transfer-CA-caller", initiated_at=0.0,
        workflow_run_id=9, warm=True, introduction="Jane is calling about her boiler",
    )
    base.update(kw)
    return TransferContext(**base)


class FormRequest:
    def __init__(self, data):
        self._data = data

    async def form(self):
        return self._data

    async def json(self):
        return self._data


# --- the colleague's leg ---------------------------------------------------------


def test_cold_twiml_is_unchanged():
    assert warm_transfer.twilio_transfer_twiml("transfer-CA1") == """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>You have answered a transfer call. Connecting you now.</Say>
    <Dial>
        <Conference endConferenceOnExit="true">transfer-CA1</Conference>
    </Dial>
</Response>"""


def test_warm_twiml_reads_the_introduction_and_waits_for_a_key():
    twiml = warm_transfer.twilio_transfer_twiml(
        "transfer-CA1", "Jane <Doe> & co wants a quote", "https://x/accept/t1?a=1&b=2"
    )
    assert '<Gather input="dtmf" numDigits="1"' in twiml
    assert 'action="https://x/accept/t1?a=1&amp;b=2"' in twiml
    assert "Jane &lt;Doe&gt; &amp; co wants a quote. Press 1 to take the call." in twiml
    assert "<Conference" not in twiml  # nobody is connected before they press 1
    assert twiml.rstrip().endswith("</Response>") and "<Redirect" in twiml


def test_introductions_are_tidied_and_capped():
    assert warm_transfer.clean_introduction("  a \n b ") == "a b"
    assert warm_transfer.clean_introduction("   ") is None
    assert len(warm_transfer.clean_introduction("x" * 900)) == warm_transfer.MAX_INTRODUCTION_CHARS


def test_destinations_are_masked_in_the_call_history():
    assert warm_transfer.masked_destination("+14155550123") == "…0123"
    assert warm_transfer.masked_destination("PJSIP/1001@pbx") == "PJSIP/1001"


def test_older_and_newer_contexts_both_load():
    old = json.dumps({k: v for k, v in _context().to_dict().items() if k not in {"warm", "introduction", "answered", "accepted", "resolved"}})
    assert TransferContext.from_json(old).warm is False
    newer = json.dumps({**_context().to_dict(), "something_later": 1})
    assert TransferContext.from_json(newer).introduction.startswith("Jane")


# --- Twilio ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_twilio_answering_a_warm_leg_is_not_taking_the_call():
    from api.routes import telephony

    store = FakeTransfers(_context())
    with patch.object(telephony, "get_call_transfer_manager", AsyncMock(return_value=store)):
        result = await telephony.complete_transfer_function_call(
            "t1", FormRequest({"CallStatus": "in-progress", "CallSid": "CA-dest"})
        )
        assert result == {"status": "pending"} and store.events == []

        # ...hung up (or voicemail) without pressing 1 = declined, reported once
        await telephony.complete_transfer_function_call(
            "t1", FormRequest({"CallStatus": "completed", "CallSid": "CA-dest"})
        )
        await telephony.complete_transfer_function_call(
            "t1", FormRequest({"CallStatus": "completed", "CallSid": "CA-dest"})
        )
    assert [(e.type, e.reason) for e in store.events] == [(TransferEventType.TRANSFER_FAILED, "declined")]


@pytest.mark.asyncio
async def test_twilio_cold_transfers_still_connect_on_answer():
    from api.routes import telephony

    store = FakeTransfers(_context(warm=False, introduction=None))
    with patch.object(telephony, "get_call_transfer_manager", AsyncMock(return_value=store)):
        await telephony.complete_transfer_function_call(
            "t1", FormRequest({"CallStatus": "in-progress", "CallSid": "CA-dest"})
        )
    assert store.events[0].type == TransferEventType.DESTINATION_ANSWERED


@pytest.mark.asyncio
@pytest.mark.parametrize("digits,accepted", [("1", True), ("2", False), ("", False)])
async def test_twilio_pressing_1_connects_and_anything_else_declines(digits, accepted):
    from api.routes import telephony

    store = FakeTransfers(_context())
    with patch.object(telephony, "get_call_transfer_manager", AsyncMock(return_value=store)):
        response = await telephony.accept_warm_transfer(
            "t1", FormRequest({"Digits": digits, "CallSid": "CA-dest"})
        )
        again = await telephony.accept_warm_transfer("t1", FormRequest({"Digits": "1"}))
    body = response.body.decode()
    assert ("<Conference" in body) is accepted
    assert "<Conference" not in again.body.decode()  # one outcome per transfer
    assert len(store.events) == 1
    event = store.events[0]
    assert event.type == (TransferEventType.DESTINATION_ANSWERED if accepted else TransferEventType.TRANSFER_FAILED)
    assert store.contexts["t1"].accepted is accepted and store.contexts["t1"].resolved


# --- Telnyx ----------------------------------------------------------------------


def _telnyx_event(event_type, **payload):
    return FormRequest({"data": {"event_type": event_type, "payload": {"call_control_id": "cc-dest", **payload}}})


@pytest.mark.asyncio
async def test_telnyx_warm_leg_reads_the_introduction_then_connects_on_1():
    from api.services.telephony.providers.telnyx import routes

    store = FakeTransfers(_context())
    provider = Mock(call_action=AsyncMock(return_value=True))
    with patch.object(routes, "get_call_transfer_manager", AsyncMock(return_value=store)), \
         patch.object(routes, "_resolve_telnyx_provider", AsyncMock(return_value=provider)), \
         patch.object(routes, "_seed_destination_conference", AsyncMock(return_value="conf-1")) as seed:
        assert await routes.handle_telnyx_transfer_result("t1", _telnyx_event("call.answered")) == {"status": "pending"}
        action, payload = provider.call_action.await_args.args[1:]
        assert action == "gather_using_speak"
        assert "Jane is calling about her boiler. Press 1" in payload["payload"]
        assert store.events == [] and seed.await_count == 0

        await routes.handle_telnyx_transfer_result("t1", _telnyx_event("call.gather.ended", digits="1"))
    assert seed.await_count == 1
    assert store.events[0].type == TransferEventType.DESTINATION_ANSWERED
    assert store.contexts["t1"].conference_id == "conf-1"


@pytest.mark.asyncio
async def test_telnyx_warm_leg_declined_or_hung_up_is_reported_once():
    from api.services.telephony.providers.telnyx import routes

    for final in (_telnyx_event("call.gather.ended", digits="2"), _telnyx_event("call.hangup", hangup_cause="normal_clearing")):
        store = FakeTransfers(_context())
        provider = Mock(call_action=AsyncMock(return_value=True))
        with patch.object(routes, "get_call_transfer_manager", AsyncMock(return_value=store)), \
             patch.object(routes, "_resolve_telnyx_provider", AsyncMock(return_value=provider)):
            await routes.handle_telnyx_transfer_result("t1", _telnyx_event("call.answered"))
            await routes.handle_telnyx_transfer_result("t1", final)
            await routes.handle_telnyx_transfer_result("t1", _telnyx_event("call.hangup", hangup_cause="normal_clearing"))
        assert [(e.type, e.reason) for e in store.events] == [(TransferEventType.TRANSFER_FAILED, "declined")]


@pytest.mark.asyncio
async def test_telnyx_warm_leg_that_never_answers_is_no_answer():
    from api.services.telephony.providers.telnyx import routes

    store = FakeTransfers(_context())
    with patch.object(routes, "get_call_transfer_manager", AsyncMock(return_value=store)):
        await routes.handle_telnyx_transfer_result("t1", _telnyx_event("call.hangup", hangup_cause="timeout"))
    assert store.events[0].reason == "no_answer"


# --- the transfer tool -----------------------------------------------------------


def test_a_warm_transfer_tool_asks_the_model_for_an_introduction():
    from api.services.workflow.tools.custom_tool import tool_to_function_schema

    def schema(config):
        tool = SimpleNamespace(tool_uuid="tool", name="Transfer Call", description="Transfer", definition={"type": "transfer_call", "config": config})
        return tool_to_function_schema(tool)["function"]["parameters"]

    warm = schema({"destination": "+15550001111", "transfer_mode": "warm"})
    assert "introduction" in warm["properties"] and warm["required"] == ["introduction"]
    assert "introduction" not in schema({"destination": "+15550001111"})["properties"]


def test_the_tools_api_keeps_the_new_settings():
    from api.routes.tool import TransferCallConfig

    config = TransferCallConfig(destination="+15550001111", transfer_mode="warm", fallback_destination="+15550002222")
    assert config.model_dump()["fallback_destination"] == "+15550002222"
    with pytest.raises(ValueError):
        TransferCallConfig(destination="+15550001111", fallback_destination="not a number")


def _transfer_manager(provider, store, mode="ari"):
    from api.services.workflow import pipecat_engine_custom_tools as tools_module

    engine = Mock()
    engine._workflow_run_id = 9
    engine._audio_config = None
    engine._transport_output = Mock(queue_frame=AsyncMock())
    engine.log_phone_tool_event = AsyncMock()
    engine.end_call_with_reason = AsyncMock()
    manager = tools_module.CustomToolManager(engine)
    manager.get_organization_id = AsyncMock(return_value=1)
    manager._play_config_message = AsyncMock(return_value=False)
    run = SimpleNamespace(mode="twilio", gathered_context={"call_id": "CA-caller"}, initial_context={})
    patches = [
        patch.object(tools_module.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)),
        patch.object(tools_module, "is_sandbox_test_run", lambda run: False),
        patch.object(tools_module, "get_telephony_provider_for_run", AsyncMock(return_value=provider)),
        patch.object(tools_module, "get_call_transfer_manager", AsyncMock(return_value=store)),
        patch.object(tools_module, "play_audio_loop", AsyncMock()),
    ]
    return manager, engine, patches


def _provider(warm=True):
    return Mock(
        supports_transfers=lambda: True, validate_config=lambda: True,
        supports_warm_transfer=lambda: warm,
        transfer_call=AsyncMock(return_value={"call_sid": "CA-dest"}),
    )


def _tool(**config):
    return SimpleNamespace(tool_uuid="tool", definition={"type": "transfer_call", "config": {"destination": "+15550001111", **config}})


def _answered(transfer_id):
    return TransferEvent(type=TransferEventType.DESTINATION_ANSWERED, transfer_id=transfer_id,
                         original_call_sid="CA-caller", status="success", action="destination_answered")


def _failed(reason):
    return lambda transfer_id: TransferEvent(type=TransferEventType.TRANSFER_FAILED, transfer_id=transfer_id,
                                             original_call_sid="CA-caller", status="transfer_failed",
                                             action="transfer_failed", reason=reason)


@pytest.mark.asyncio
async def test_the_backup_number_is_tried_when_the_first_person_does_not_take_the_call():
    store = FakeTransfers()
    store.outcomes = [_failed("declined"), _answered]
    provider = _provider()
    manager, engine, patches = _transfer_manager(provider, store)
    handler = manager._create_transfer_call_handler(_tool(transfer_mode="warm", fallback_destination="+15550002222"), "transfer_call")
    result = AsyncMock()
    for p in patches: p.start()
    try:
        await handler(SimpleNamespace(arguments={"introduction": "Jane, boiler service"}, result_callback=result))
    finally:
        for p in patches: p.stop()
    dialled = [c.kwargs for c in provider.transfer_call.await_args_list]
    assert [d["destination"] for d in dialled] == ["+15550001111", "+15550002222"]
    assert all(d["introduction"] == "Jane, boiler service" for d in dialled)
    outcomes = [c.kwargs for c in engine.log_phone_tool_event.await_args_list]
    assert [(o["destination"], o["outcome"], o["warm"]) for o in outcomes] == [("…1111", "declined", True), ("…2222", "connected", True)]
    assert result.await_args.args[0]["status"] == "transfer_success"
    engine.end_call_with_reason.assert_awaited_once()
    assert len(store.contexts) == 1  # the declined attempt was removed; the connected one stays


@pytest.mark.asyncio
async def test_when_nobody_takes_the_call_the_agent_offers_a_message():
    store = FakeTransfers()
    store.outcomes = [None]
    provider = _provider(warm=False)
    manager, engine, patches = _transfer_manager(provider, store)
    handler = manager._create_transfer_call_handler(_tool(transfer_mode="warm"), "transfer_call")
    result = AsyncMock()
    for p in patches: p.start()
    try:
        await handler(SimpleNamespace(arguments={"introduction": "Jane"}, result_callback=result))
    finally:
        for p in patches: p.stop()
    # This carrier can't introduce the caller, so it rang them cold.
    assert "introduction" not in provider.transfer_call.await_args.kwargs
    sent = result.await_args.args[0]
    assert sent["status"] == "transfer_failed" and sent["reason"] == "timeout"
    assert "take a message" in sent["next_step"]
    engine.end_call_with_reason.assert_not_awaited()
    engine.set_mute_pipeline.assert_called_with(False)
