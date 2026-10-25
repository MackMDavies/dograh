"""Play a saved caller script against native transport speaking events.

Completion proves the prescribed audio exchange occurred. It does not judge
transcript correctness, task success, pronunciation or carrier latency.
"""

import asyncio
import hashlib
from dataclasses import dataclass

from api.services.workflow.voice_test_control import VoiceControlError, VoiceSession
from api.services.workflow.voice_test_audio_evidence import PrivateVoiceAudioEvidence
from api.services.workflow.voice_test_timing_evidence import PrivateVoiceTimingEvidence
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.frame_processor import FrameDirection


@dataclass
class AudioResponse:
    sequence: int
    started_at: float
    stopped_at: float | None = None
    first_audio_at: float | None = None


class VoiceTurnObserver(BaseObserver):
    """Observe only this private output; never retain transcript/tool contents."""

    def __init__(self, output):
        super().__init__()
        self.output = output
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.responses: list[AudioResponse] = []
        self.closed = False
        self.failure: str | None = None
        self.tool_trace = None
        self.audio_evidence = PrivateVoiceAudioEvidence()
        self.timing_evidence = PrivateVoiceTimingEvidence(self.responses)

    @property
    def current(self):
        return self.responses[-1] if self.responses else None

    async def on_pipeline_started(self):
        self.timing_evidence.observe(asyncio.get_running_loop().time())
        self.ready.set()
        self.changed.set()

    def observe_output(self, frame, direction):
        # Called synchronously by this private output before queuing observer
        # copies. Pipeline observers run asynchronously, so their callback time
        # would incorrectly include observer queue delay and could miss PCM.
        if direction != FrameDirection.DOWNSTREAM:
            return
        now = asyncio.get_running_loop().time()
        self.timing_evidence.observe(now)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self.timing_evidence.record_turn_start(now, "vad_user_started")
            self.changed.set()
            return
        if isinstance(frame, UserStartedSpeakingFrame):
            self.timing_evidence.record_turn_start(now, "user_turn_started")
            self.changed.set()
            return
        current = self.current
        if isinstance(frame, BotStartedSpeakingFrame):
            if current is None or current.stopped_at is not None:
                if len(self.responses) >= 128:
                    self.failure = "voice_response_limit_exceeded"
                else:
                    self.responses.append(AudioResponse(len(self.responses) + 1, now))
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if current is not None and current.stopped_at is None:
                current.stopped_at = now
        elif isinstance(frame, InterruptionFrame):
            self.timing_evidence.record_interruption(now,
                current.sequence if current is not None and current.stopped_at is None else None)
        elif isinstance(frame, ErrorFrame):
            self.failure = "voice_pipeline_failed"
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self.closed = True
        else:
            return
        self.changed.set()

    def record_audio(self, at: float, size: int, *, pcm_start_byte=None):
        """Called by the private PCM writer, at actual output, after validation."""
        current = self.current
        self.audio_evidence.record(pcm_start_byte, size,
                                   current.sequence if current is not None and current.stopped_at is None else None)
        sequence = current.sequence if current is not None and current.stopped_at is None else None
        self.timing_evidence.record_output(at, size, sequence, pcm_start_byte=pcm_start_byte)
        if (
            size
            and current is not None
            and current.stopped_at is None
            and current.first_audio_at is None
        ):
            current.first_audio_at = at
            self.changed.set()
        return sequence

    async def wait_for(self, predicate, deadline: float):
        while True:
            self.changed.clear()
            if self.failure:
                raise VoiceControlError(self.failure)
            if predicate():
                return
            if self.closed:
                raise VoiceControlError("voice_pipeline_ended_early")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise VoiceControlError("voice_response_timeout")
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except TimeoutError:
                raise VoiceControlError("voice_response_timeout") from None


async def run_voice_script(
    session: VoiceSession,
    fixtures: dict[str, bytes],
    transport,
    observer: VoiceTurnObserver,
):
    """Execute only verified saved inputs; the caller owns pipeline teardown.

    A response is a transport speech segment, bounded by speaking events. These
    measurements must not be presented as independently evaluated turn quality.
    """
    script = session.script
    if script is None:
        raise VoiceControlError("voice_script_missing")
    if (
        observer.output is not transport.output()
        or transport.voice_observer is not observer
    ):
        raise VoiceControlError("voice_observer_not_attached")
    if set(fixtures) != {fixture.file_name for fixture in session.fixtures}:
        raise VoiceControlError("voice_fixture_not_planned")
    for fixture in session.fixtures:
        data = fixtures[fixture.file_name]
        if (
            not isinstance(data, bytes)
            or hashlib.sha256(data).hexdigest() != fixture.sha256
        ):
            raise VoiceControlError("voice_fixture_readback_invalid")
    loop = asyncio.get_running_loop()
    # Readiness wait consumes the same hard rehearsal envelope. The native
    # transport has its own independent cap even if the driver is cancelled.
    deadline = loop.time() + session.duration_seconds
    await observer.wait_for(observer.ready.is_set, deadline)
    pending = None
    response_deadline = min(deadline, loop.time() + script.opening_timeout_ms / 1000)
    if script.start_mode == "agent_first":
        await observer.wait_for(lambda: observer.current is not None, response_deadline)
        pending = observer.current
        await observer.wait_for(
            lambda: pending.first_audio_at is not None, response_deadline
        )
    evidence = []
    for step in script.steps:
        if pending is not None:
            if step.delivery == "after_response":
                await observer.wait_for(
                    lambda response=pending: response.stopped_at is not None,
                    response_deadline,
                )
            elif pending.stopped_at is not None:
                raise VoiceControlError("voice_interruption_window_missed")
        if loop.time() + step.delay_ms / 1000 >= deadline:
            raise VoiceControlError("voice_duration_limit_reached")
        await asyncio.sleep(step.delay_ms / 1000)
        if observer.failure:
            raise VoiceControlError(observer.failure)
        if observer.closed:
            raise VoiceControlError("voice_pipeline_ended_early")
        if step.delivery == "interrupt" and (
            pending is None or pending.stopped_at is not None
        ):
            raise VoiceControlError("voice_interruption_window_missed")
        if loop.time() >= deadline:
            raise VoiceControlError("voice_duration_limit_reached")
        baseline = len(observer.responses)
        if observer.tool_trace is not None:
            observer.tool_trace.begin_caller_step(step.file_name)
        try:
            async with asyncio.timeout_at(deadline):
                await transport.play_wav(fixtures[step.file_name], fixture_id=step.file_name)
        except TimeoutError:
            raise VoiceControlError("voice_duration_limit_reached") from None
        caller_finished_at = loop.time()
        response_deadline = min(
            deadline, caller_finished_at + step.response_timeout_ms / 1000
        )
        await observer.wait_for(
            lambda previous=baseline: len(observer.responses) > previous,
            response_deadline,
        )
        pending = observer.responses[baseline]
        await observer.wait_for(
            lambda response=pending: response.first_audio_at is not None,
            response_deadline,
        )
        evidence.append(
            {
                "file_name": step.file_name,
                "response_sequence": pending.sequence,
                # A negative value records overlapping speech; it must not be
                # counted as a successful low-latency, non-interrupted turn.
                "caller_end_to_first_audio_ms": round(
                    (pending.first_audio_at - caller_finished_at) * 1000, 3
                ),
                "delivery": step.delivery,
            }
        )
    await observer.wait_for(lambda: pending.stopped_at is not None, response_deadline)
    if transport.failure:
        raise VoiceControlError("voice_pipeline_failed")
    return {
        "status": "script_executed",
        "turns": evidence,
        "measurement_scope": "saved caller PCM to native output speech segments; excludes carrier and network latency",
        "quality_evaluated": False,
    }
