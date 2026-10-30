"""Caller key presses reach the agent (Keypad & Native Phone Tools, phase 1)."""

import pytest

from api.services.pipecat.caller_keypad import CallerKeypadProcessor
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    InputDTMFFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
)
from pipecat.tests.utils import SleepFrame, run_test


def _press(*keys: str):
    return [InputDTMFFrame(button=KeypadEntry(k)) for k in keys]


def _recording_handler(seen: list[str], reply=None):
    async def handler(keys: str):
        seen.append(keys)
        return reply

    return handler


@pytest.mark.asyncio
async def test_presses_are_dropped_until_the_engine_installs_a_handler():
    processor = CallerKeypadProcessor(idle_timeout=0.05)
    down, _ = await run_test(processor, frames_to_send=[*_press("1"), SleepFrame(0.1)])
    assert not any(isinstance(f, (InputDTMFFrame, InterruptionFrame)) for f in down)


@pytest.mark.asyncio
async def test_hash_completes_the_entry_at_once():
    seen: list[str] = []
    processor = CallerKeypadProcessor(idle_timeout=5)
    processor.set_handler(_recording_handler(seen))
    await run_test(processor, frames_to_send=_press("1", "2", "#"))
    assert seen == ["12#"]


@pytest.mark.asyncio
async def test_a_pause_completes_the_entry():
    seen: list[str] = []
    processor = CallerKeypadProcessor(idle_timeout=0.05)
    processor.set_handler(_recording_handler(seen))
    await run_test(processor, frames_to_send=[*_press("1"), SleepFrame(0.2), *_press("2"), SleepFrame(0.2)])
    assert seen == ["1", "2"]


@pytest.mark.asyncio
async def test_the_first_press_interrupts_the_agent_and_the_reply_goes_downstream():
    reply = LLMMessagesAppendFrame([{"role": "user", "content": "pressed"}], run_llm=True)
    processor = CallerKeypadProcessor(idle_timeout=5)
    processor.set_handler(_recording_handler([], reply))
    down, _ = await run_test(processor, frames_to_send=_press("1", "#"))
    kinds = [type(f) for f in down]
    assert kinds.count(InterruptionFrame) == 1
    assert LLMMessagesAppendFrame in kinds
    assert not any(isinstance(f, InputDTMFFrame) for f in down)


@pytest.mark.asyncio
async def test_presses_are_ignored_while_the_agent_cannot_be_interrupted():
    seen: list[str] = []
    processor = CallerKeypadProcessor(idle_timeout=5)
    processor.set_handler(_recording_handler(seen), accepting=lambda: False)
    down, _ = await run_test(processor, frames_to_send=_press("1", "#"))
    assert seen == []
    assert not any(isinstance(f, InterruptionFrame) for f in down)


@pytest.mark.asyncio
async def test_long_entries_are_capped_and_a_failing_handler_is_contained():
    seen: list[str] = []
    processor = CallerKeypadProcessor(idle_timeout=5)
    processor.set_handler(_recording_handler(seen))
    await run_test(processor, frames_to_send=_press(*("1" * 40), "#"))
    assert len(seen[0]) == 32

    async def broken(keys: str):
        raise RuntimeError("boom")

    processor = CallerKeypadProcessor(idle_timeout=5)
    processor.set_handler(broken)
    await run_test(processor, frames_to_send=_press("1", "#"))


@pytest.mark.asyncio
async def test_asterisk_presses_are_injected_into_the_same_path():
    seen: list[str] = []
    processor = CallerKeypadProcessor(idle_timeout=5)
    processor.set_handler(_recording_handler(seen))
    queued = []

    async def fake_queue(frame, direction=None):
        queued.append(frame)

    processor.queue_frame = fake_queue
    await processor.inject_key("x")
    await processor.inject_key("4")
    assert [f.button.value for f in queued] == ["4"]
