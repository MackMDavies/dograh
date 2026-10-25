"""Shared protocol: cancellation is separate from nominal submitted PCM duration."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
