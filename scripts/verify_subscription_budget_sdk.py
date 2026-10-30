"""Offline call-deadline probe against the deployed Pipecat SDK.

Run with the backend dependencies installed, or bundle to stdout with --bundle
and pass that output to a disposable container using the inspected live image.
No credentials, network, database or provider calls are needed. This proves SDK
cancellation of a blocked processor; it does not prove carrier hangup.
"""
from pathlib import Path
import sys

if __name__ == "__main__" and sys.argv[1:] == ["--bundle"]:
    probe = Path(__file__).read_text()
    exercise = probe.split("# BEGIN SDK EXERCISE\n", 1)[1]
    source = Path(__file__).resolve().parents[1] / "api/services/subscription_call_budget.py"
    print("BUDGET_SOURCE = " + repr(source.read_text()))
    print(exercise)
    raise SystemExit(0)

if __name__ == "__main__" and sys.argv[1:]:
    raise SystemExit("Usage: verify_subscription_budget_sdk.py [--bundle]")

BUDGET_SOURCE = (Path(__file__).resolve().parents[1] /
    "api/services/subscription_call_budget.py").read_text()

# BEGIN SDK EXERCISE
import signal
signal.alarm(25)
import asyncio
import json
import sys
import time
import types

from pipecat.frames.frames import CancelFrame, StartFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask, PipelineTaskParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.utils.enums import EndTaskReason

budget_module = types.ModuleType('isolated_subscription_budget')
sys.modules[budget_module.__name__] = budget_module
exec(compile(BUDGET_SOURCE, 'candidate-subscription-call-budget.py', 'exec'), budget_module.__dict__)


class BlockedResponse(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.blocked = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cancel_time = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        if isinstance(frame, TextFrame):
            self.blocked.set()
            await asyncio.Event().wait()  # A model response that never arrives.
        if isinstance(frame, CancelFrame):
            self.cancel_time = time.monotonic()
            self.cancelled.set()
        await self.push_frame(frame, direction)


async def run():
    processor = BlockedResponse()
    pipeline = Pipeline([processor])
    task = PipelineTask(pipeline, params=PipelineParams(enable_heartbeats=False),
        enable_tracing=False, enable_rtvi=False)
    calls = []
    start = time.monotonic()
    budget = budget_module.CallBudget(101, 1, start + .4)

    async def stop():
        calls.append(time.monotonic())
        await task.cancel(reason=EndTaskReason.CALL_DURATION_EXCEEDED.value)

    running = asyncio.create_task(budget_module.run_with_call_budget(
        lambda: task.run(PipelineTaskParams(loop=asyncio.get_running_loop())), stop, budget,
        shutdown_grace=.5))
    await asyncio.wait_for(processor.started.wait(), 3)
    await task.queue_frame(TextFrame('synthetic blocked response'))
    await asyncio.wait_for(processor.blocked.wait(), 3)
    await asyncio.wait_for(running, 5)
    assert len(calls) == 1, 'The deadline must request cancellation exactly once'
    assert processor.cancelled.is_set(), 'The actual SDK never processed CancelFrame'
    assert processor.cancel_time - start < 1, 'Cancellation waited for the blocked model response'
    print(json.dumps({'sdk_cancel_frame_processed': True, 'heartbeats': False,
        'blocked_model_response': True, 'elapsed_seconds': round(processor.cancel_time-start, 3),
        'provider_calls': 0, 'production_modified': False}), flush=True)


asyncio.run(run())
