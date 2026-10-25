"""Shared protocol: cancellation is separate from nominal submitted PCM duration."""
import copy
import asyncio
import json
from pathlib import Path
import pytest
from types import SimpleNamespace

from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_timing_evidence import validate_voice_timing_evidence


def protocol():
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v2.json").read_text())
    audio = json.loads((Path(__file__).parent / "fixtures/voiceAudioEvidence.v1.json").read_text())
    return value["timing"], [SimpleNamespace(**item) for item in value["fixtures"]], audio["evidence"]["spans"]


def parse(value):
    _, fixtures, spans = protocol()
    return validate_voice_timing_evidence(value, duration_ms=1000, fixtures=fixtures, spans=spans)


def test_cancelled_packet_and_active_caller_interruption_remain_distinct():
    value, _, _ = protocol()
    assert parse(value) is value
    assert value["responses"][0]["stopped_ms"] < value["responses"][0]["last_pcm_end_ms"]
    assert value["interruptions"][0]["caller_active"] is True


def test_v3_records_only_bounded_callback_policy_changes_and_marks_other_strategies_unobserved():
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v3.json").read_text())
    timing = value["timing"]
    _, fixtures, spans = protocol()
    assert validate_voice_timing_evidence(timing, duration_ms=1000, fixtures=fixtures, spans=spans) is timing
    assert timing["callback_mute_policy"]["other_mute_strategies_observed"] is False
    assert [sample["state"] for sample in timing["callback_mute_policy"]["samples"]] == [
        "unmuted", "node_forbids_interruption"
    ]


def test_v4_persists_the_three_fixed_strategy_sources_with_bounded_decisions():
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v4.json").read_text())
    timing = value["timing"]
    _, fixtures, spans = protocol()
    assert validate_voice_timing_evidence(timing, duration_ms=1000, fixtures=fixtures, spans=spans) is timing
    assert timing["mute_policy"]["strategies"] == ["first_bot_complete", "function_call", "workflow_callback"]
    assert timing["mute_policy"]["samples"][5]["reason"] == "node_forbids_interruption"


def test_v5_binds_turn_start_source_and_signal_to_caller_frames():
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v5.json").read_text())
    timing = value["timing"]
    _, fixtures, spans = protocol()
    assert validate_voice_timing_evidence(timing, duration_ms=1000, fixtures=fixtures, spans=spans) is timing
    assert timing["turn_detection"]["source"] == "realtime_external_events"
    assert [signal["signal"] for signal in timing["turn_detection"]["signals"]] == [
        "user_turn_started", "vad_user_started"
    ]
    timing["turn_detection"]["signals"] = []
    assert parse(timing) is timing


@pytest.mark.asyncio
async def test_runtime_mute_callback_records_the_node_policy_and_its_actual_return_value():
    from api.services.pipecat.observed_user_mute_strategy import ObservedUserMuteStrategy
    from api.services.workflow.pipecat_engine import PipecatEngine
    from api.services.workflow.voice_test_timing_evidence import PrivateVoiceTimingEvidence
    from pipecat.frames.frames import (
        BotStoppedSpeakingFrame,
        FunctionCallFromLLM,
        FunctionCallResultFrame,
        FunctionCallsStartedFrame,
        UserStartedSpeakingFrame,
    )
    from pipecat.turns.user_mute import (
        CallbackUserMuteStrategy,
        FunctionCallUserMuteStrategy,
        MuteUntilFirstBotCompleteUserMuteStrategy,
    )

    response = SimpleNamespace(sequence=1, started_at=1.0, stopped_at=None)
    timing = PrivateVoiceTimingEvidence([response])
    timing.observe(asyncio.get_running_loop().time())
    caller = timing.begin_caller("caller-001.wav", "c" * 64, 640)
    first = asyncio.get_running_loop().time()
    timing.record_input(caller, first, 0, 640, 640)
    engine = PipecatEngine.__new__(PipecatEngine)
    engine._queued_speech_mute_state = "idle"
    engine._bot_is_speaking = True
    engine._current_node = SimpleNamespace(allow_interrupt=False)
    engine._mute_pipeline = False
    engine._mute_active_since = None
    engine._max_bot_mute_seconds = 15.0
    engine._voice_test_mute_observation_enabled = True
    engine._voice_test_mute_reason = None
    first_strategy = ObservedUserMuteStrategy(MuteUntilFirstBotCompleteUserMuteStrategy(), "first_bot_complete", timing)
    function_strategy = ObservedUserMuteStrategy(FunctionCallUserMuteStrategy(), "function_call", timing)
    callback = CallbackUserMuteStrategy(should_mute_callback=engine.should_mute_user)
    callback_strategy = ObservedUserMuteStrategy(callback, "workflow_callback", timing,
        reason=lambda: engine._voice_test_mute_reason,
        node_allows_interrupt=lambda: getattr(engine._current_node, "allow_interrupt", None))

    assert await first_strategy.process_frame(UserStartedSpeakingFrame()) is True
    assert await first_strategy.process_frame(BotStoppedSpeakingFrame()) is False
    call = FunctionCallFromLLM("lookup", "opaque-id", {}, None)
    assert await function_strategy.process_frame(FunctionCallsStartedFrame([call])) is True
    assert await function_strategy.process_frame(FunctionCallResultFrame("lookup", "opaque-id", {}, {})) is False
    assert await callback_strategy.process_frame(UserStartedSpeakingFrame()) is True
    engine._current_node.allow_interrupt = True
    assert await callback_strategy.process_frame(UserStartedSpeakingFrame()) is False
    samples = timing.snapshot(pipeline_stopped=False)["mute_policy"]["samples"]
    assert [(sample["strategy"], sample["muted"]) for sample in samples] == [
        ("first_bot_complete", True), ("first_bot_complete", False),
        ("function_call", True), ("function_call", False),
        ("workflow_callback", True), ("workflow_callback", False),
    ]
    assert [sample["node_allows_interrupt"] for sample in samples if sample["strategy"] == "workflow_callback"] == [False, True]


@pytest.mark.asyncio
async def test_private_voice_observer_records_only_turn_start_frame_kinds():
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from api.services.workflow.voice_test_timing_evidence import PrivateVoiceTimingEvidence
    from pipecat.frames.frames import UserStartedSpeakingFrame, VADUserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    observer = VoiceTurnObserver(None)
    observer.timing_evidence.set_turn_detection_source("realtime_external_events")
    now = asyncio.get_running_loop().time()
    observer.timing_evidence.observe(now)
    caller = observer.timing_evidence.begin_caller("caller-001.wav", "c" * 64, 640)
    observer.timing_evidence.record_input(caller, now, 0, 640, 640)
    observer.observe_output(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    observer.observe_output(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    observer.timing_evidence.finish_input(caller, now + 0.02)
    signals = observer.timing_evidence.snapshot(pipeline_stopped=False)["turn_detection"]["signals"]
    assert [signal["signal"] for signal in signals] == ["user_turn_started", "vad_user_started"]


@pytest.mark.parametrize("change", [
    lambda v: v["callback_mute_policy"].update(other_mute_strategies_observed=True),
    lambda v: v["callback_mute_policy"]["samples"][0].update(state="allow_interrupt_true"),
    lambda v: v["callback_mute_policy"]["samples"][0].update(at_ms=21),
    lambda v: v["callback_mute_policy"]["samples"][1].update(caller_step=1),
    lambda v: v["callback_mute_policy"]["samples"][1].update(sequence=3),
    lambda v: v["callback_mute_policy"]["samples"][0].update(sequence=True),
    lambda v: v["callback_mute_policy"]["samples"][0].update(state={"private": True}),
    lambda v: v["callback_mute_policy"]["samples"].append(dict(v["callback_mute_policy"]["samples"][1], sequence=3)),
])
def test_v3_rejects_policy_samples_that_are_unbounded_or_ambiguous(change):
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v3.json").read_text())["timing"]
    change(value)
    with pytest.raises(VoiceControlError, match="voice_timing_evidence_invalid"):
        parse(value)


@pytest.mark.parametrize("change", [
    lambda v: v["mute_policy"].update(strategies=["function_call", "first_bot_complete", "workflow_callback"]),
    lambda v: v["mute_policy"]["samples"].pop(),
    lambda v: v["mute_policy"]["samples"][0].update(muted=1),
    lambda v: v["mute_policy"]["samples"][0].update(reason="private"),
    lambda v: v["mute_policy"]["samples"][2].update(reason="node_forbids_interruption"),
    lambda v: v["mute_policy"]["samples"][2].update(muted=True),
    lambda v: v["mute_policy"]["samples"][2].update(sequence=True),
    lambda v: v["mute_policy"]["samples"][0].update(at_ms=21),
])
def test_v4_rejects_incomplete_or_inconsistent_strategy_evidence(change):
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v4.json").read_text())["timing"]
    change(value)
    with pytest.raises(VoiceControlError, match="voice_timing_evidence_invalid"):
        parse(value)


@pytest.mark.parametrize("change", [
    lambda v: v["turn_detection"].update(source="provider_private_name"),
    lambda v: v["turn_detection"].update(source=[]),
    lambda v: v["turn_detection"]["signals"][0].update(sequence=True),
    lambda v: v["turn_detection"]["signals"][0].update(at_ms=21),
    lambda v: v["turn_detection"]["signals"][0].update(caller_step=0),
    lambda v: v["turn_detection"]["signals"][0].update(signal="private"),
    lambda v: v["turn_detection"]["signals"].append("private"),
])
def test_v5_rejects_unknown_or_unbound_turn_source_and_signal(change):
    value = json.loads((Path(__file__).parent / "fixtures/voiceTimingEvidence.v5.json").read_text())["timing"]
    change(value)
    with pytest.raises(VoiceControlError, match="voice_timing_evidence_invalid"):
        parse(value)


@pytest.mark.parametrize("change", [
    lambda v: v.update(version=1), lambda v: v.update(version=True), lambda v: v.update(scope="native delivery"),
    lambda v: v["responses"][0].update(cancelled_write=None), lambda v: v["responses"][0].pop("cancelled_write"),
    lambda v: v["responses"][0]["cancelled_write"].update(pcm_start_byte=-2),
    lambda v: v["responses"][0]["cancelled_write"].update(pcm_start_byte=1),
    lambda v: v["responses"][0]["cancelled_write"].update(pcm_end_byte=62),
    lambda v: v["responses"][0]["cancelled_write"].update(pcm_end_byte=128),
    lambda v: v["responses"][0]["cancelled_write"].update(pcm_start_byte=64, pcm_end_byte=128),
    lambda v: v["responses"][0]["cancelled_write"].update(submitted_ms=15),
    lambda v: v["responses"][0]["cancelled_write"].update(cancelled_ms=15),
    lambda v: v["responses"][0]["cancelled_write"].update(cancelled_ms=18),
    lambda v: v["responses"][0]["cancelled_write"].update(transcript="private"),
    lambda v: v["responses"][0].update(last_pcm_end_ms=19), lambda v: v["responses"][0].update(last_pcm_end_ms=float("inf")),
    lambda v: v.update(interruptions=None), lambda v: v["interruptions"][0].update(sequence=True),
    lambda v: v["interruptions"][0].update(sequence=2), lambda v: v["interruptions"][0].update(at_ms=-1),
    lambda v: v["interruptions"][0].update(at_ms=14), lambda v: v["interruptions"][0].update(at_ms=18),
    lambda v: v["interruptions"][0].update(response_sequence=True), lambda v: v["interruptions"][0].update(response_sequence=128),
    lambda v: v["interruptions"][0].update(caller_step=-1), lambda v: v["interruptions"][0].update(caller_step=0),
    lambda v: v["interruptions"][0].update(caller_step=2), lambda v: v["interruptions"][0].update(caller_active=1),
    lambda v: v["interruptions"][0].update(text="private"),
    lambda v: v["interruptions"][0].update(response_sequence=None, caller_active=False, caller_step=0),
    lambda v: v["interruptions"][0].update(caller_active=False, caller_step=2),
    lambda v: v["interruptions"][0].update(caller_active=False, caller_step=1, at_ms=85, response_sequence=2),
    lambda v: v["interruptions"][0].update(caller_active=False),
    lambda v: v["interruptions"][0].update(at_ms=25, response_sequence=None),
    lambda v: v.update(interruptions=[dict(v["interruptions"][0], sequence=i + 1) for i in range(129)]),
])
def test_inconsistent_cancelled_tail_or_unbound_event_cannot_be_retained(change):
    value, _, _ = protocol()
    change(value)
    with pytest.raises(VoiceControlError, match="voice_timing_evidence_invalid"):
        parse(value)


def test_only_a_bound_cancelled_tail_can_extend_past_the_observed_session_end():
    value, _, _ = protocol()
    value["responses"][1].update(started_ms=998, first_pcm_ms=999, last_pcm_end_ms=1001, stopped_ms=999.5,
        cancelled_write=dict(pcm_start_byte=64, pcm_end_byte=128, submitted_ms=999, cancelled_ms=999.25))
    assert parse(value) is value
    value["responses"][1]["cancelled_write"] = None
    with pytest.raises(VoiceControlError):
        parse(value)


def test_idle_signal_does_not_invent_an_interrupted_response_or_active_caller():
    value, _, _ = protocol()
    value["interruptions"].insert(0, dict(sequence=1, at_ms=0, response_sequence=None, caller_step=0, caller_active=False))
    value["interruptions"][1]["sequence"] = 2
    assert parse(value) is value


def test_sealed_reader_preserves_v2_while_the_status_contract_remains_v1():
    from api.tests.test_voice_audio_evidence import protocol_fixture
    from api.services.workflow.voice_test_audio_evidence import make_voice_audio_evidence_report
    value, report, session = protocol_fixture()
    timing, fixtures, _ = protocol()
    session.fixtures = fixtures
    value["evidence"]["timing"] = copy.deepcopy(timing)
    assert make_voice_audio_evidence_report(report, session, value["evidence"]) == value
    assert report["version"] == 1
