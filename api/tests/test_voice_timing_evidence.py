"""Timing must come from actual paced transports, not a concatenated WAV."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_actual_writer_preserves_a_delivery_gap_missing_from_the_wav():
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, OutputAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection

    transport = SandboxAudioTransport(duration_limit=5)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    writer = transport.output()
    await writer.push_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await writer.write_audio_frame(OutputAudioRawFrame(b"\x01\x00" * 320, 16000, 1))
    await asyncio.sleep(0.06)
    await writer.write_audio_frame(OutputAudioRawFrame(b"\x02\x00" * 320, 16000, 1))
    await writer.push_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    timing = observer.timing_evidence.snapshot(pipeline_stopped=True)
    assert len(transport.capture.pcm) == 1280  # WAV remains exactly 40 ms.
    assert len(timing["responses"]) == 1
    segment = timing["responses"][0]
    assert segment["last_pcm_end_ms"] - segment["first_pcm_ms"] >= 95
    assert segment["delivery_gap_count"] == 1
    assert segment["max_delivery_gap_ms"] >= 50
    assert segment["total_delivery_gap_ms"] >= 50


@pytest.mark.asyncio
async def test_actual_sdk_interruption_can_seal_the_packet_cancelled_during_pacing():
    """The whole packet was submitted; cancellation need not wait its nominal end."""
    import hashlib
    from pipecat.frames.frames import InterruptionFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.frame_processor import FrameDirection
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from api.services.workflow.voice_test_timing_evidence import validate_voice_timing_evidence
    from api.tests.test_sandbox_audio_transport import wav_fixture
    from api.tests.test_voice_test_rehearsal import FixtureResponder

    transport = SandboxAudioTransport(duration_limit=5)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    observer.timing_evidence.set_turn_detection_source("vad_transcription_speech_timeout")
    submitted = asyncio.Event()
    original = observer.record_audio

    def record(*args, **kwargs):
        sequence = original(*args, **kwargs)
        submitted.set()
        return sequence

    observer.record_audio = record
    original_input = observer.timing_evidence.record_input
    def record_input(item, at, offset, size, padded_size):
        original_input(item, at, offset, size, padded_size)
        if not observer.timing_evidence.turn_signals:
            observer.timing_evidence.record_turn_start(at, "user_turn_started")
        observer.timing_evidence.record_strategy_mute(at, "first_bot_complete", False)
        observer.timing_evidence.record_strategy_mute(at, "function_call", False)
        observer.timing_evidence.record_strategy_mute(at, "workflow_callback", False, "unmuted")
    observer.timing_evidence.record_input = record_input
    task = PipelineTask(Pipeline([transport.input(), FixtureResponder(), transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000), observers=[observer])
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    encoded, _ = wav_fixture()
    try:
        await transport.play_wav(encoded, fixture_id="caller-001.wav")
        await asyncio.wait_for(submitted.wait(), timeout=2)
        # Run the pinned SDK's real sender interruption path, including cancellation.
        await transport.output().process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        assert observer.current.stopped_at is not None
        timing = observer.timing_evidence.snapshot(pipeline_stopped=True)
        segment = timing["responses"][0]
        assert segment["stopped_ms"] < segment["last_pcm_end_ms"]
        spans = observer.audio_evidence.snapshot(pcm_bytes=len(transport.capture.pcm),
            responses=observer.responses, pipeline_stopped=True)["spans"]
        fixtures = [SimpleNamespace(file_name="caller-001.wav", sha256=hashlib.sha256(encoded).hexdigest(), duration_ms=80)]
        assert validate_voice_timing_evidence(timing, duration_ms=5000, fixtures=fixtures, spans=spans) is timing
        assert timing["version"] == 5
        assert segment["cancelled_write"]["pcm_end_byte"] == spans[0]["pcm_end_byte"]
        assert segment["cancelled_write"]["cancelled_ms"] <= segment["stopped_ms"]
        assert timing["interruptions"][0]["response_sequence"] == 1
        assert timing["complete"] is True
    finally:
        await task.cancel()
        await asyncio.wait_for(running, timeout=5)


@pytest.mark.asyncio
async def test_actual_input_records_verified_fixture_identity_and_padded_playback():
    import hashlib
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from api.tests.test_sandbox_audio_transport import wav_fixture

    encoded, pcm = wav_fixture()
    transport = SandboxAudioTransport(duration_limit=5)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    input_ = transport.input()
    input_.started.set()
    input_.deadline = asyncio.get_running_loop().time() + 5
    # Drive the actual input stream, discarding frames only after submission.
    async def consume(frame):
        assert frame.audio
    input_.push_audio_frame = consume
    stream = asyncio.create_task(input_._stream())
    try:
        await transport.play_wav(encoded, fixture_id="caller-001.wav")
        timing = observer.timing_evidence.snapshot(pipeline_stopped=True)
        assert len(timing["caller_steps"]) == 1
        caller = timing["caller_steps"][0]
        assert caller["file_name"] == "caller-001.wav"
        assert caller["sha256"] == hashlib.sha256(encoded).hexdigest()
        assert caller["pcm_bytes"] == caller["submitted_pcm_bytes"] == len(pcm)
        assert caller["last_frame_end_ms"] - caller["first_frame_ms"] >= len(pcm) / 32 - 0.01
        assert caller["playback_finished_ms"] >= caller["last_frame_end_ms"] - 0.01
    finally:
        input_.closed = True
        await stream


@pytest.mark.asyncio
async def test_input_submission_marks_active_caller_before_a_downstream_signal_can_run():
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from api.tests.test_sandbox_audio_transport import wav_fixture
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    transport = SandboxAudioTransport(duration_limit=5)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    input_ = transport.input()
    input_.started.set()
    input_.deadline = asyncio.get_running_loop().time() + 5
    observer.observe_output(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    observer.record_audio(asyncio.get_running_loop().time(), 640, pcm_start_byte=0)

    async def submit(frame):
        if any(frame.audio):
            # Accepted input can yield to downstream processing before its
            # submitting coroutine resumes after the await.
            observer.observe_output(InterruptionFrame(), FrameDirection.DOWNSTREAM)
            await asyncio.sleep(0)

    input_.push_audio_frame = submit
    stream = asyncio.create_task(input_._stream())
    try:
        await transport.play_wav(wav_fixture(seconds=0.02)[0], fixture_id="caller-001.wav")
        observer.observe_output(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        timing = observer.timing_evidence.snapshot(pipeline_stopped=True)
        event = timing["interruptions"][0]
        caller = timing["caller_steps"][0]
        assert caller["first_frame_ms"] <= event["at_ms"] <= caller["playback_finished_ms"]
        assert event["caller_step"] == 1
        assert event["caller_active"] is True
    finally:
        input_.closed = True
        await stream


@pytest.mark.asyncio
async def test_failed_input_submission_cannot_certify_the_prepared_frame():
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from api.tests.test_sandbox_audio_transport import wav_fixture

    transport = SandboxAudioTransport(duration_limit=5)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    input_ = transport.input()
    input_.started.set()
    input_.deadline = asyncio.get_running_loop().time() + 5
    async def reject(frame):
        if any(frame.audio):
            raise RuntimeError("synthetic input submission rejected")
    input_.push_audio_frame = reject
    playing = asyncio.create_task(transport.play_wav(wav_fixture(seconds=0.02)[0], fixture_id="caller-001.wav"))
    stream = asyncio.create_task(input_._stream())
    try:
        with pytest.raises(RuntimeError, match="synthetic input submission rejected"):
            await stream
    finally:
        await input_._close()
        with pytest.raises(RuntimeError, match="ended during fixture playback"):
            await playing
    assert observer.timing_evidence.invalid is True
    timing = observer.timing_evidence.snapshot(pipeline_stopped=True)
    assert timing["complete"] is False
    assert timing["caller_steps"] == []


def protocol():
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v1.json").read_text())
    audio = json.loads((Path(__file__).parent / "fixtures/voiceAudioEvidence.v1.json").read_text())
    return value["timing"], [SimpleNamespace(**item) for item in value["fixtures"]], audio["evidence"]["spans"]


def test_actual_sealing_accepts_shared_timing_and_keeps_missing_timing_unverified():
    from api.services.workflow.voice_test_audio_evidence import make_voice_audio_evidence_report
    from api.tests.test_voice_audio_evidence import protocol_fixture
    timing, fixtures, _ = protocol()
    value, report, session = protocol_fixture()
    session.fixtures = fixtures
    value["evidence"]["timing"] = copy.deepcopy(timing)
    assert make_voice_audio_evidence_report(report, session, value["evidence"]) == value
    del value["evidence"]["timing"]
    assert "timing" not in make_voice_audio_evidence_report(report, session, value["evidence"])["evidence"]


@pytest.mark.parametrize("change", [
    lambda t: t.update(complete=1), lambda t: t.update(version=True),
    lambda t: t.update(scope="caller intelligence"), lambda t: t.update(transcript="private"),
    lambda t: t.update(caller_steps=[]), lambda t: t.update(responses=[]),
    lambda t: t["caller_steps"][0].update(sha256="e"*64),
    lambda t: t["caller_steps"][0].update(file_name="caller-002.wav"),
    lambda t: t["caller_steps"][0].update(pcm_bytes=642),
    lambda t: t["caller_steps"][0].update(submitted_pcm_bytes=638),
    lambda t: t["caller_steps"][0].update(submitted_pcm_bytes=True),
    lambda t: t["caller_steps"][0].update(first_frame_ms=-1),
    lambda t: t["caller_steps"][0].update(playback_finished_ms=19),
    lambda t: t["caller_steps"][0].update(last_frame_end_ms=19),
    lambda t: t["caller_steps"][1].update(first_frame_ms=19),
    lambda t: t["responses"][0].update(pcm_bytes=66),
    lambda t: t["responses"][0].update(response_sequence=True),
    lambda t: t["responses"][1].update(response_sequence=1),
    lambda t: t["responses"][1].update(started_ms=28),
    lambda t: t["responses"][0].update(first_pcm_ms=24),
    lambda t: t["responses"][0].update(first_pcm_ms=float("nan")),
    lambda t: t["responses"][0].update(first_pcm_ms=True),
    lambda t: t["responses"][0].update(last_pcm_end_ms=27),
    lambda t: t["responses"][0].update(stopped_ms=1001),
    lambda t: t["responses"][0].update(delivery_gap_count=-1),
    lambda t: t["responses"][0].update(delivery_gap_count=True),
    lambda t: t["responses"][0].update(delivery_gap_count=1),
    lambda t: t["responses"][0].update(max_delivery_gap_ms=1),
    lambda t: t["responses"][0].update(total_delivery_gap_ms=1),
    lambda t: t["responses"][0].update(audio="private"),
])
def test_inconsistent_or_unbound_timing_is_rejected(change):
    from api.services.workflow.voice_test_timing_evidence import validate_voice_timing_evidence
    from api.services.workflow.voice_test_control import VoiceControlError
    value, fixtures, spans = protocol()
    change(value)
    with pytest.raises(VoiceControlError, match="voice_timing_evidence_invalid"):
        validate_voice_timing_evidence(value, duration_ms=1000, fixtures=fixtures, spans=spans)


def test_delivery_gaps_are_bounded_by_the_actual_response_elapsed_time():
    from api.services.workflow.voice_test_timing_evidence import validate_voice_timing_evidence
    value, fixtures, spans = protocol()
    value["responses"][0].update(last_pcm_end_ms=55, stopped_ms=56, delivery_gap_count=1,
                                 max_delivery_gap_ms=27, total_delivery_gap_ms=27)
    assert validate_voice_timing_evidence(value, duration_ms=1000, fixtures=fixtures, spans=spans) is value


def test_unfinished_input_or_response_cannot_make_a_complete_timing_claim():
    from api.services.workflow.voice_test_timing_evidence import PrivateVoiceTimingEvidence
    response = SimpleNamespace(sequence=1, started_at=1, stopped_at=None)
    evidence = PrivateVoiceTimingEvidence([response])
    evidence.observe(1)
    evidence.record_output(1.01, 640, 1)
    caller = evidence.begin_caller("caller-001.wav", "c"*64, 640)
    evidence.set_turn_detection_source("vad_transcription_speech_timeout")
    evidence.record_input(caller, 1.1, 0, 640, 640)
    evidence.record_turn_start(1.105, "user_turn_started")
    evidence.record_strategy_mute(1.11, "first_bot_complete", False)
    evidence.record_strategy_mute(1.11, "function_call", False)
    evidence.record_mute_policy(1.11, "unmuted")
    assert evidence.snapshot(pipeline_stopped=True)["complete"] is False
    response.stopped_at = 1.04
    evidence.finish_input(caller, 1.12)
    assert evidence.snapshot(pipeline_stopped=False)["complete"] is False
    assert evidence.snapshot(pipeline_stopped=True)["complete"] is True
    evidence.begin_caller("caller-001.wav", "c"*64, 640)
    assert evidence.snapshot(pipeline_stopped=True)["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_job_saves_and_reads_actual_transport_timing_with_the_sealed_audio(interrupt, monkeypatch):
    import hashlib
    from dataclasses import replace
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from api.tests.test_sandbox_audio_transport import wav_fixture
    from api.tests.test_voice_test_job import setup
    from api.tests.test_voice_test_rehearsal import FixtureResponder, FixtureInterruptResponder
    from api.tests.test_voice_test_control import SECRET
    from api.services.workflow.voice_test_control import VoiceFixture
    from api.services.workflow.voice_test_job import execute_voice_job, read_voice_job_audio_evidence
    from api.services.workflow import voice_test_audio_evidence

    rejected = []
    original = voice_test_audio_evidence.validate_voice_timing_evidence
    def validate_timing(value, **kwargs):
        try:
            return original(value, **kwargs)
        except Exception:
            rejected.append(copy.deepcopy(value))
            raise
    monkeypatch.setattr(voice_test_audio_evidence, "validate_voice_timing_evidence", validate_timing)

    encoded, _ = wav_fixture()
    context, db, control, run, _, _, _ = setup()
    session = control.get.return_value
    session = replace(session, duration_seconds=5,
        fixtures=(VoiceFixture("caller-001.wav", hashlib.sha256(encoded).hexdigest(), 80),),
        script=replace(session.script, start_mode="agent_first" if interrupt else "caller_first",
            steps=(replace(session.script.steps[0], response_timeout_ms=2000, delivery="interrupt" if interrupt else "after_response"),)))
    control.get.return_value = session
    control.fixture.return_value = encoded

    async def actual_transport(transport, *_):
        responder = FixtureInterruptResponder() if interrupt else FixtureResponder()
        observer = transport.voice_observer
        observer.timing_evidence.set_turn_detection_source("vad_transcription_speech_timeout")
        original_input = observer.timing_evidence.record_input
        turn_recorded = False
        def record_input(item, at, offset, size, padded_size):
            nonlocal turn_recorded
            original_input(item, at, offset, size, padded_size)
            if not turn_recorded:
                observer.timing_evidence.record_turn_start(at, "user_turn_started")
                turn_recorded = True
            observer.timing_evidence.record_strategy_mute(at, "first_bot_complete", False)
            observer.timing_evidence.record_strategy_mute(at, "function_call", False)
            observer.timing_evidence.record_strategy_mute(at, "workflow_callback", False, "unmuted")
        observer.timing_evidence.record_input = record_input
        task = PipelineTask(
            Pipeline([transport.input(), responder, transport.output()]),
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
            observers=[observer],
        )
        transport.pipeline_task = task
        await PipelineRunner(handle_sigint=False).run(task)

    result = await execute_voice_job(db, control, context, SECRET, pipeline_runner=actual_transport)
    assert result["status"] == "completed", dict(result=result, rejected_timing=rejected)
    report = run.annotations["agent_test_voice_result"]
    control.get.return_value = replace(session, status="completed", runtime_workflow_run_id=123,
        actual_duration_ms=report["actual_duration_ms"], audio_bytes=report["audio_bytes"], audio_sha256=report["audio_sha256"])
    value = await read_voice_job_audio_evidence(db, control, context, SECRET)
    assert value["evidence"]["complete"] is True
    assert value["evidence"]["timing"]["complete"] is True
    assert value["evidence"]["timing"]["caller_steps"][0]["sha256"] == session.fixtures[0].sha256
    if interrupt:
        timing = value["evidence"]["timing"]
        assert timing["version"] == 5
        assert timing["interruptions"][0]["response_sequence"] == 1
        assert timing["interruptions"][0]["caller_step"] == 1
        assert timing["interruptions"][0]["caller_active"] is True
        assert report["rehearsal"]["turns"][0]["response_sequence"] == 2
        assert report["rehearsal"]["turns"][0]["delivery"] == "interrupt"
    assert value["audio_sha256"] == hashlib.sha256(control.upload.await_args.args[3]).hexdigest()
    # Native status v1 still says quality_evaluated=false; timing cannot certify speech.
    assert report["rehearsal"]["quality_evaluated"] is False
