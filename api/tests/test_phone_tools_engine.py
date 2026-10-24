"""The engine applies the organization's phone tool rules (Keypad & Native Phone Tools)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.services.phone_tools.policy import normalize_policy
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_context_composer import compose_functions_for_node
from pipecat.frames.frames import LLMMessagesAppendFrame

_NODE = SimpleNamespace(document_uuids=[], tool_uuids=[], out_edges=[])


def _names(functions):
    return {getattr(f, "name", None) for f in functions}


def _description(functions, name):
    return next(f.description for f in functions if getattr(f, "name", None) == name)


def _engine(*, realtime=False):
    engine = PipecatEngine(
        llm=Mock(queue_frame=AsyncMock()),
        workflow=Mock(),
        call_context_vars={},
        workflow_run_id=1,
        is_realtime=realtime,
    )
    return engine


@pytest.mark.asyncio
async def test_press_keys_is_offered_by_default_and_can_be_switched_off():
    default = await compose_functions_for_node(node=_NODE, custom_tool_manager=None, kb_document_uuids=[])
    assert "press_keys" in _names(default)
    off = await compose_functions_for_node(
        node=_NODE, custom_tool_manager=None, kb_document_uuids=[],
        phone_tools_policy=normalize_policy({"tools": {"keypad": {"mode": "off"}}}),
    )
    assert "press_keys" not in _names(off)


@pytest.mark.asyncio
async def test_a_keypad_rule_reaches_the_model_in_the_tool_description():
    functions = await compose_functions_for_node(
        node=_NODE, custom_tool_manager=None, kb_document_uuids=[],
        phone_tools_policy=normalize_policy(
            {"tools": {"keypad": {"mode": "rule", "rule": "only on outbound calls"}}}
        ),
    )
    assert "only on outbound calls" in _description(functions, "press_keys")


def test_the_caller_keypad_is_only_connected_when_allowed():
    keypad = Mock()
    engine = _engine()
    engine.set_phone_tools(normalize_policy(None), caller_keypad=keypad)
    handler, accepting = keypad.set_handler.call_args.args
    assert handler == engine._on_caller_keys and accepting == engine._caller_keys_accepted

    engine.set_phone_tools(
        normalize_policy({"tools": {"caller_keypad": {"mode": "off"}}}), caller_keypad=keypad
    )
    assert keypad.set_handler.call_args.args == (None,)


@pytest.mark.asyncio
async def test_cascaded_agents_get_the_keys_through_the_user_aggregator():
    engine = _engine()
    logs = Mock(append=AsyncMock())
    engine.set_phone_tools(normalize_policy(None), logs_buffer=logs)
    frame = await engine._on_caller_keys("1")
    assert isinstance(frame, LLMMessagesAppendFrame) and frame.run_llm
    assert "pressed these keys on their phone keypad: 1" in frame.messages[0]["content"]
    engine.llm.queue_frame.assert_not_awaited()
    assert logs.append.await_args.args[0] == {
        "type": "phone_tool", "payload": {"tool": "caller_keypad", "keys": "1"},
    }
    assert engine._gathered_context["caller_keys_pressed"] == ["1"]


@pytest.mark.asyncio
async def test_realtime_agents_get_the_keys_directly():
    engine = _engine(realtime=True)
    engine.set_phone_tools(
        normalize_policy({"tools": {"caller_keypad": {"mode": "rule", "rule": "to confirm a booking"}}})
    )
    assert await engine._on_caller_keys("2") is None
    sent = engine.llm.queue_frame.await_args.args[0]
    assert isinstance(sent, LLMMessagesAppendFrame) and sent.run_llm
    assert "to confirm a booking" in sent.messages[0]["content"]


def test_keys_follow_the_same_mute_rules_as_speech():
    engine = _engine()
    assert engine._caller_keys_accepted()
    engine._current_node = SimpleNamespace(allow_interrupt=False)
    engine._bot_is_speaking = True
    assert not engine._caller_keys_accepted()
    engine._current_node = SimpleNamespace(allow_interrupt=True)
    assert engine._caller_keys_accepted()
    engine.set_mute_pipeline(True)
    assert not engine._caller_keys_accepted()


@pytest.mark.asyncio
async def test_asterisk_key_presses_reach_the_engine_and_nothing_else_does():
    from api.services.supervisor.live_control import SupervisorControl, caller_keypad_command

    engine = Mock(inject_caller_key=AsyncMock())
    control = SupervisorControl(engine, 5, 1)
    await control._handle(caller_keypad_command("7"))
    await control._handle(caller_keypad_command("hello"))
    engine.inject_caller_key.assert_awaited_once_with("7")


def test_the_keypad_sits_right_after_the_transport_input_in_both_pipelines():
    from api.services.pipecat.caller_keypad import CallerKeypadProcessor
    from api.services.pipecat.pipeline_builder import build_pipeline, build_realtime_pipeline
    from pipecat.processors.frame_processor import FrameProcessor

    def stage(name):
        return FrameProcessor(name=name)

    transport = Mock(input=lambda: stage("in"), output=lambda: stage("out"))
    keypad = CallerKeypadProcessor()
    cascaded = build_pipeline(
        transport, stage("stt"), stage("buffer"), stage("llm"), stage("tts"), stage("user"),
        stage("assistant"), stage("callbacks"), stage("metrics"), caller_keypad=keypad,
    )
    keypad2 = CallerKeypadProcessor()
    realtime = build_realtime_pipeline(
        transport, stage("rt"), stage("buffer"), stage("user"), stage("assistant"),
        stage("callbacks"), stage("metrics"), caller_keypad=keypad2,
    )
    for pipeline, pad in ((cascaded, keypad), (realtime, keypad2)):
        names = [p.name for p in pipeline.processors]
        assert names.index(pad.name) == names.index("in") + 1


@pytest.mark.asyncio
async def test_the_agents_key_presses_show_in_the_call_history():
    engine = _engine()
    engine.task = Mock(queue_frame=AsyncMock())
    logs = Mock(append=AsyncMock())
    engine.set_phone_tools(normalize_policy(None), logs_buffer=logs)
    engine.llm.register_function = Mock()
    await engine._register_dtmf_function()
    press_keys = engine.llm.register_function.call_args.args[1]
    result = AsyncMock()
    await press_keys(SimpleNamespace(arguments={"digits": "9"}, result_callback=result))
    assert logs.append.await_args.args[0] == {
        "type": "phone_tool", "payload": {"tool": "keypad", "keys": "9"},
    }
    assert result.await_args.args[0]["status"] == "pressed"
