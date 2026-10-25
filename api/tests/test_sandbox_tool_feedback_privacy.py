"""Exercise private tool feedback before either persistence or live streaming."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pipecat.frames.frames import (
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.pipecat.in_memory_buffers import InMemoryLogsBuffer
from api.services.pipecat.realtime_feedback_observer import RealtimeFeedbackObserver


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["start", "end"])
@pytest.mark.parametrize("stream", [False, True])
async def test_sandbox_tool_values_never_reach_events_or_frame_logs(kind, stream):
    sender = AsyncMock() if stream else None
    buffer = InMemoryLogsBuffer(123)
    buffer.set_current_node("booking", "Booking")
    observer = RealtimeFeedbackObserver(sender, buffer, redact_tool_values=True)
    arguments = {"email": "private-caller@example.invalid"}
    if kind == "start":
        frame = FunctionCallInProgressFrame("book_appointment", "call-1", arguments)
    else:
        frame = FunctionCallResultFrame(
            "book_appointment", "call-1", arguments,
            {"provider_secret": "private-provider-value"},
        )
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), level="TRACE")
    try:
        pushed = SimpleNamespace(frame=frame, direction=FrameDirection.DOWNSTREAM)
        await observer.on_push_frame(pushed)
        await observer.on_push_frame(pushed)  # Multiple processors see the same frame.
        events = buffer.get_events()
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["function_name"] == "book_appointment"
        assert payload["tool_call_id"] == "call-1"
        assert payload["values_redacted"] is True
        assert "arguments" not in payload
        if kind == "end":
            assert payload["result"] is None
        assert events[0]["node_id"] == "booking"
        if sender:
            sender.assert_awaited_once()
            assert sender.call_args.args[0]["payload"] == payload
        encoded = json.dumps(events) + "".join(logs)
        assert "private-caller@example.invalid" not in encoded
        assert "private-provider-value" not in encoded
        assert frame.arguments is arguments  # Runtime inputs are never modified.
    finally:
        logger.remove(sink)
        await observer.cleanup()


@pytest.mark.asyncio
async def test_sandbox_does_not_copy_or_serialize_values_even_when_they_are_opaque():
    class Opaque:
        def __str__(self):
            raise AssertionError("Private result was serialized")

        def __repr__(self):
            raise AssertionError("Private frame was formatted")

        def __iter__(self):
            raise AssertionError("Private arguments were copied")

    buffer = InMemoryLogsBuffer(123)
    observer = RealtimeFeedbackObserver(None, buffer, redact_tool_values=True)
    for frame in [
        FunctionCallInProgressFrame("book_appointment", "call-1", Opaque()),
        FunctionCallResultFrame("book_appointment", "call-1", Opaque(), Opaque()),
    ]:
        await observer.on_push_frame(SimpleNamespace(frame=frame, direction=FrameDirection.DOWNSTREAM))
    assert len(buffer.get_events()) == 2
    await observer.cleanup()


@pytest.mark.asyncio
async def test_sandbox_batch_frame_diagnostics_do_not_format_raw_arguments():
    observer = RealtimeFeedbackObserver(None, redact_tool_values=True)
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), level="TRACE")
    try:
        frame = FunctionCallsStartedFrame([
            FunctionCallFromLLM("lookup", "call-1", {"email": "private@example.invalid"}, None)
        ])
        await observer.on_push_frame(SimpleNamespace(frame=frame, direction=FrameDirection.DOWNSTREAM))
        assert logs
        assert "private@example.invalid" not in "".join(logs)
    finally:
        logger.remove(sink)
        await observer.cleanup()


@pytest.mark.asyncio
async def test_ordinary_feedback_retains_its_existing_tool_value_contract():
    sender = AsyncMock()
    observer = RealtimeFeedbackObserver(sender)
    for frame in [
        FunctionCallInProgressFrame("lookup", "call-1", {"query": "ordinary input"}),
        FunctionCallResultFrame("lookup", "call-1", {}, {"found": True}),
    ]:
        await observer.on_push_frame(SimpleNamespace(frame=frame, direction=FrameDirection.DOWNSTREAM))
    assert sender.call_args_list[0].args[0]["payload"]["arguments"] == {"query": "ordinary input"}
    assert sender.call_args_list[1].args[0]["payload"]["result"] == "{'found': True}"
    assert "values_redacted" not in sender.call_args_list[0].args[0]["payload"]
    await observer.cleanup()


@pytest.mark.parametrize("sandbox", [False, True])
def test_actual_pipeline_selects_redaction_only_for_its_verified_sandbox(sandbox):
    # Execute the real construction expression without starting providers or a call.
    source = Path(__file__).parents[1] / "services/pipecat/run_pipeline.py"
    tree = ast.parse(source.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "RealtimeFeedbackObserver"]
    assert len(calls) == 1
    scope = {"RealtimeFeedbackObserver": lambda **kwargs: kwargs,
             "ws_sender": None, "in_memory_logs_buffer": object(),
             "is_agent_test_sandbox": sandbox}
    expression = ast.Expression(body=calls[0])
    kwargs = eval(compile(expression, str(source), "eval"), scope)
    assert kwargs["redact_tool_values"] is sandbox
