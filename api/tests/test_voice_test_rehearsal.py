import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
from api.services.pipecat.voice_test_rehearsal import (
    VoiceTurnObserver,
    run_voice_script,
)
from api.services.workflow.voice_test_control import VoiceControlError, VoiceSession
from api.tests.test_sandbox_audio_transport import wav_fixture
from api.tests.test_voice_test_control import SESSION, metadata, wav_bytes


def setup(*, mode="caller_first", delivery="after_response", next_delivery=None):
    data = metadata()
    data["script"].update(start_mode=mode, opening_timeout_ms=500)
    data["script"]["steps"][0].update(delivery=delivery, response_timeout_ms=500)
    if next_delivery:
        data["fixture_manifest"].append(
            {**data["fixture_manifest"][0], "file_name": "caller-002.wav"}
        )
        data["script"]["steps"].append(
            {
                **data["script"]["steps"][0],
                "file_name": "caller-002.wav",
                "delivery": next_delivery,
            }
        )
    session = VoiceSession.parse(data, SESSION, 185)
    output = object()
    observer = VoiceTurnObserver(output)
    played = []
    response_tasks = []

    async def speak():
        observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        observer.record_audio(asyncio.get_running_loop().time(), 640)
        await asyncio.sleep(0.04)
        observer.observe_output(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    async def play(encoded, *, fixture_id=None):
        played.append(encoded)
        current = observer.current
        if current and current.stopped_at is None:
            observer.observe_output(
                BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM
            )
        response_tasks.append(asyncio.create_task(speak()))

    transport = SimpleNamespace(
        output=lambda: output, voice_observer=observer, play_wav=play, failure=None
    )
    fixtures = {fixture.file_name: wav_bytes() for fixture in session.fixtures}
    return session, fixtures, transport, observer, played, response_tasks


@pytest.mark.asyncio
async def test_observer_counts_only_output_speech_and_actual_pcm_and_bounds_memory():
    observer = VoiceTurnObserver(object())
    observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert observer.current is None
    observer.record_audio(1, 640)
    assert observer.current is None
    for index in range(128):
        observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        observer.record_audio(index + 1, 0)
        assert observer.current.first_audio_at is None
        observer.record_audio(index + 1, 640)
        observer.record_audio(index + 2, 640)
        assert observer.current.first_audio_at == index + 1
        observer.observe_output(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert len(observer.responses) == 128
    assert observer.failure == "voice_response_limit_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,next_delivery",
    [
        ("caller_first", None),
        ("agent_first", "after_response"),
        ("agent_first", "interrupt"),
    ],
)
async def test_saved_script_drives_complete_exchanges_without_claiming_quality(
    mode, next_delivery
):
    session, fixtures, transport, observer, played, tasks = setup(
        mode=mode, next_delivery=next_delivery
    )
    await observer.on_pipeline_started()
    if mode == "agent_first":
        observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        observer.record_audio(asyncio.get_running_loop().time(), 640)
        observer.observe_output(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    result = await run_voice_script(session, fixtures, transport, observer)
    await asyncio.gather(*tasks)
    assert len(played) == len(session.script.steps) == len(result["turns"])
    assert (
        result["status"] == "script_executed" and result["quality_evaluated"] is False
    )
    assert "excludes carrier" in result["measurement_scope"]
    assert all(
        "transcript" not in turn and turn["response_sequence"] > 0
        for turn in result["turns"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["missing_script", "missing_fixture", "hash", "observer"]
)
async def test_preflight_rejects_unplanned_input_before_playback(bad):
    session, fixtures, transport, observer, played, _ = setup()
    if bad == "missing_script":
        session = replace(session, script=None)
    elif bad == "missing_fixture":
        fixtures.clear()
    elif bad == "hash":
        fixtures["caller-001.wav"] += b"x"
    else:
        transport.voice_observer = None
    with pytest.raises(VoiceControlError):
        await run_voice_script(session, fixtures, transport, observer)
    assert played == []


@pytest.mark.asyncio
async def test_missing_opening_times_out_without_playing_a_caller():
    session, fixtures, transport, observer, played, _ = setup(mode="agent_first")
    await observer.on_pipeline_started()
    with pytest.raises(VoiceControlError, match="voice_response_timeout"):
        await run_voice_script(session, fixtures, transport, observer)
    assert played == []


@pytest.mark.asyncio
async def test_interruption_cannot_pass_when_the_agent_has_already_stopped():
    session, fixtures, transport, observer, played, _ = setup(
        mode="agent_first", delivery="interrupt"
    )
    await observer.on_pipeline_started()
    observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    observer.record_audio(asyncio.get_running_loop().time(), 640)
    observer.observe_output(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    with pytest.raises(VoiceControlError, match="voice_interruption_window_missed"):
        await run_voice_script(session, fixtures, transport, observer)
    assert played == []


@pytest.mark.asyncio
async def test_opening_speaking_events_without_actual_pcm_cannot_advance_the_script():
    session, fixtures, transport, observer, played, _ = setup(mode="agent_first")
    await observer.on_pipeline_started()
    observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    observer.observe_output(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    with pytest.raises(VoiceControlError, match="voice_response_timeout"):
        await run_voice_script(session, fixtures, transport, observer)
    assert played == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", [EndFrame(), ErrorFrame("private provider detail", fatal=True)]
)
async def test_pipeline_end_and_errors_do_not_become_success_or_expose_provider_details(
    ending,
):
    session, fixtures, transport, observer, played, _ = setup()
    observer.observe_output(ending, FrameDirection.DOWNSTREAM)
    with pytest.raises(VoiceControlError) as error:
        await run_voice_script(session, fixtures, transport, observer)
    assert "private provider detail" not in str(error.value)
    assert played == []


@pytest.mark.asyncio
async def test_cancellation_does_not_leave_a_driver_wait_running():
    session, fixtures, transport, observer, played, _ = setup(mode="agent_first")
    running = asyncio.create_task(
        run_voice_script(session, fixtures, transport, observer)
    )
    await asyncio.sleep(0)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert played == []


@pytest.mark.asyncio
async def test_closed_ready_pipeline_never_receives_a_new_caller_fixture():
    session, fixtures, transport, observer, played, _ = setup()
    await observer.on_pipeline_started()
    observer.observe_output(EndFrame(), FrameDirection.DOWNSTREAM)
    with pytest.raises(VoiceControlError, match="voice_pipeline_ended_early"):
        await run_voice_script(session, fixtures, transport, observer)
    assert played == []


@pytest.mark.asyncio
async def test_no_response_after_caller_is_a_timeout_and_not_a_pass():
    session, fixtures, transport, observer, played, _ = setup()
    await observer.on_pipeline_started()

    async def no_reply(encoded, *, fixture_id=None):
        played.append(encoded)

    transport.play_wav = no_reply
    with pytest.raises(VoiceControlError, match="voice_response_timeout"):
        await run_voice_script(session, fixtures, transport, observer)
    assert len(played) == 1 and observer.responses == []


class FixtureResponder(FrameProcessor):
    """Transport wiring proof, with no recognition, synthesis or model calls."""

    def __init__(self):
        super().__init__()
        self.receiving = False
        self.received = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if any(frame.audio):
                self.receiving = True
                self.received.append(frame.audio)
            elif self.receiving:
                self.receiving = False
                # Native output uses 40 ms chunks; keep this transport proof
                # aligned so the test does not depend on trailing TTS padding.
                await self.push_frame(TTSAudioRawFrame(b"\x01\x00" * 1280, 16000, 1))
                await self.push_frame(TTSStoppedFrame())
        else:
            await self.push_frame(frame, direction)


class FixtureInterruptResponder(FrameProcessor):
    """Synthetic speaker/VAD stand-in, using actual SDK output cancellation."""

    def __init__(self):
        super().__init__()
        self.receiving = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            await self.push_frame(TTSAudioRawFrame(b"\x01\x00" * 16000, 16000, 1))
            await self.push_frame(TTSStoppedFrame())
        elif isinstance(frame, InputAudioRawFrame):
            if any(frame.audio) and not self.receiving:
                self.receiving = True
                await self.push_frame(InterruptionFrame())
            elif not any(frame.audio) and self.receiving:
                self.receiving = False
                await self.push_frame(TTSAudioRawFrame(b"\x02\x00" * 1280, 16000, 1))
                await self.push_frame(TTSStoppedFrame())
        else:
            await self.push_frame(frame, direction)


@pytest.mark.asyncio
async def test_saved_script_runs_through_actual_paced_pcm_and_speaking_events():
    import hashlib

    encoded, pcm = wav_fixture()
    data = metadata()
    data["duration_seconds"] = 5
    data["fixture_manifest"][0].update(
        sha256=hashlib.sha256(encoded).hexdigest(), duration_ms=80
    )
    data["script"].update(start_mode="caller_first", opening_timeout_ms=500)
    data["script"]["steps"][0]["response_timeout_ms"] = 2000
    session = VoiceSession.parse(data, SESSION, 185)
    transport = SandboxAudioTransport(duration_limit=5)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    observer.timing_evidence.set_turn_detection_source("vad_transcription_speech_timeout")
    original_record_input = observer.timing_evidence.record_input
    def record_input(item, at, offset, size, padded_size):
        original_record_input(item, at, offset, size, padded_size)
        observer.timing_evidence.record_strategy_mute(at, "first_bot_complete", False)
        observer.timing_evidence.record_strategy_mute(at, "function_call", False)
        observer.timing_evidence.record_strategy_mute(at, "workflow_callback", False, "unmuted", True)
    observer.timing_evidence.record_input = record_input
    responder = FixtureResponder()
    task = PipelineTask(
        Pipeline([transport.input(), responder, transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
        observers=[observer],
    )
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    try:
        result = await run_voice_script(
            session, {"caller-001.wav": encoded}, transport, observer
        )
        assert result["status"] == "script_executed"
        assert b"".join(responder.received) == pcm
        assert len(transport.capture.pcm) == 2560
        assert observer.current.first_audio_at == transport.capture.first_frame_at
        assert observer.current.stopped_at is not None
        assert result["turns"][0]["caller_end_to_first_audio_ms"] >= 0
        assert result["quality_evaluated"] is False
        timing = observer.timing_evidence.snapshot(pipeline_stopped=True)
        assert timing["complete"] is True
        from api.services.workflow.voice_test_timing_evidence import validate_voice_timing_evidence
        spans = observer.audio_evidence.snapshot(
            pcm_bytes=len(transport.capture.pcm), responses=observer.responses, pipeline_stopped=True,
        )["spans"]
        assert validate_voice_timing_evidence(
            timing, duration_ms=5000, fixtures=session.fixtures, spans=spans,
        ) is timing
        assert timing["caller_steps"][0]["sha256"] == session.fixtures[0].sha256
    finally:
        await task.cancel()
        await asyncio.wait_for(running, timeout=5)


@pytest.mark.asyncio
async def test_observer_cannot_be_rebound_to_another_output_or_after_start():
    transport = SandboxAudioTransport(duration_limit=5)
    with pytest.raises(ValueError):
        transport.set_voice_observer(VoiceTurnObserver(object()))
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    with pytest.raises(ValueError):
        transport.set_voice_observer(observer)
    other = SandboxAudioTransport(duration_limit=5)
    other.input().started.set()
    with pytest.raises(ValueError):
        other.set_voice_observer(VoiceTurnObserver(other.output()))
