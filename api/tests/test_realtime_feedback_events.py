import pytest

from api.services.pipecat.realtime_feedback_events import (
    build_bot_text_event,
    build_function_call_end_event,
    build_node_transition_event,
    realtime_feedback_event_sort_key,
    stamp_realtime_feedback_event,
)


def test_build_function_call_end_event_serializes_results():
    event = build_function_call_end_event(
        function_name="lookup_contact",
        tool_call_id="tool-1",
        result={"contact_id": 42},
    )

    assert event == {
        "type": "rtf-function-call-end",
        "payload": {
            "function_name": "lookup_contact",
            "tool_call_id": "tool-1",
            "result": "{'contact_id': 42}",
        },
    }


def test_stamp_and_sort_realtime_feedback_events():
    node_transition = stamp_realtime_feedback_event(
        build_node_transition_event(
            node_id="node-1",
            node_name="Greeting",
            previous_node_id=None,
            previous_node_name=None,
        ),
        timestamp="2026-01-01T00:00:03+00:00",
        turn=0,
        node_id="node-1",
        node_name="Greeting",
    )
    bot_text = stamp_realtime_feedback_event(
        build_bot_text_event(
            text="Hello there",
            timestamp="2026-01-01T00:00:01+00:00",
        ),
        timestamp="2026-01-01T00:00:02+00:00",
        turn=0,
    )

    events = sorted([node_transition, bot_text], key=realtime_feedback_event_sort_key)

    assert events == [bot_text, node_transition]
    assert node_transition["node_id"] == "node-1"
    assert node_transition["node_name"] == "Greeting"


async def _feedback_for_frame(frame):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pipecat.processors.frame_processor import FrameDirection
    from api.services.pipecat.realtime_feedback_observer import RealtimeFeedbackObserver

    sender = AsyncMock()
    observer = RealtimeFeedbackObserver(sender)
    pushed = SimpleNamespace(frame=frame, direction=FrameDirection.DOWNSTREAM)
    await observer.on_push_frame(pushed)
    await observer.on_push_frame(pushed)  # Observers see the frame at multiple processors.
    sender.assert_awaited_once()
    await observer.cleanup()
    return sender.call_args.args[0]


@pytest.mark.asyncio
async def test_feedback_preserves_caller_identity_and_original_speech_timestamp():
    from types import SimpleNamespace
    from pipecat.frames.frames import TranscriptionFrame

    frame = TranscriptionFrame("My question", "", "2026-10-23T04:00:01Z", result=SimpleNamespace(item_id="caller-1"))
    event = await _feedback_for_frame(frame)
    assert event["payload"] == {
        "text": "My question", "final": True, "user_id": "",
        "timestamp": "2026-10-23T04:00:01Z", "item_id": "caller-1",
    }


@pytest.mark.asyncio
async def test_feedback_preserves_assistant_delta_identity_and_spacing():
    from pipecat.frames.frames import AggregationType, TTSTextFrame

    frame = TTSTextFrame("ing?", aggregated_by=AggregationType.SENTENCE)
    frame.transcript_item_id = "assistant-1"
    frame.transcript_timestamp = "2026-10-23T04:00:03Z"
    frame.includes_inter_frame_spaces = True
    event = await _feedback_for_frame(frame)
    assert event["payload"] == {
        "text": "ing?", "timestamp": "2026-10-23T04:00:03Z",
        "item_id": "assistant-1", "includes_inter_frame_spaces": True,
    }
