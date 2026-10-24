"""Actual private output boundaries; byte positions never stand in for speech quality."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_actual_pcm_writer_binds_spans_to_observed_response_boundaries():
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, OutputAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver

    transport = SandboxAudioTransport(duration_limit=5)
    writer = transport.output()
    observer = VoiceTurnObserver(writer)
    transport.set_voice_observer(observer)
    for value in [1, 2]:
        await writer.push_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for _ in range(2):
            await writer.write_audio_frame(OutputAudioRawFrame(bytes([value, 0]) * 16, 16000, 1))
        await writer.push_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    evidence = observer.audio_evidence.snapshot(
        pcm_bytes=len(transport.capture.pcm), responses=observer.responses, pipeline_stopped=True,
    )
    assert evidence["complete"] is True
    assert evidence["unattributed_pcm_bytes"] == 0
    assert evidence["spans"] == [
        {"response_sequence": 1, "pcm_start_byte": 0, "pcm_end_byte": 64},
        {"response_sequence": 2, "pcm_start_byte": 64, "pcm_end_byte": 128},
    ]
    assert bytes(transport.capture.pcm[:64]) == bytes([1, 0]) * 32
    assert bytes(transport.capture.pcm[64:]) == bytes([2, 0]) * 32
    fixture = json.loads((Path(__file__).parent / "fixtures/voiceAudioEvidence.v1.json").read_text())
    assert hashlib.sha256(transport.capture.wav_bytes()).hexdigest() == fixture["audio_sha256"]
    assert evidence == fixture["evidence"]


def protocol_fixture():
    value = json.loads((Path(__file__).parent / "fixtures/voiceAudioEvidence.v1.json").read_text())
    report = {**value, "rehearsal": {"turns": [{"response_sequence": 1}, {"response_sequence": 2}]}}
    return value, report, SimpleNamespace(scenario_id=value["scenario_id"])


def response(sequence, *, closed=True, has_audio=True):
    return SimpleNamespace(sequence=sequence, stopped_at=1 if closed else None,
                           first_audio_at=0 if has_audio else None)


def test_shared_protocol_fixture_passes_native_sealing_and_keeps_no_raw_contents():
    from api.services.workflow.voice_test_audio_evidence import make_voice_audio_evidence_report
    value, report, session = protocol_fixture()
    assert make_voice_audio_evidence_report(report, session, value["evidence"]) == value
    assert "transcript" not in value["evidence"]


@pytest.mark.parametrize("field", [
    "version", "session_id", "run_id", "scenario_id", "workflow_id", "organization_id", "runtime_workflow_run_id",
    "workflow_version", "version_kind", "configuration_hash", "runtime_workflow_hash", "audio_sha256", "audio_bytes", "actual_duration_ms",
])
def test_span_report_cannot_move_to_another_source_or_recording(field):
    from api.services.workflow.voice_test_audio_evidence import validate_voice_audio_evidence_report
    from api.services.workflow.voice_test_control import VoiceControlError
    value, report, session = protocol_fixture()
    value[field] = True if isinstance(value[field], int) else "wrong"
    with pytest.raises(VoiceControlError): validate_voice_audio_evidence_report(value, report, session)


@pytest.mark.parametrize("change", [
    lambda e: e.update(version=True), lambda e: e.update(complete=1), lambda e: e.update(truncated=True),
    lambda e: e.update(pcm_bytes=130), lambda e: e.update(unattributed_pcm_bytes=2),
    lambda e: e.update(spans=[]), lambda e: e.update(transcript="private"),
    lambda e: e["spans"][0].update(pcm_start_byte=-2), lambda e: e["spans"][0].update(pcm_end_byte=63),
    lambda e: (e["spans"][0].update(pcm_end_byte=63), e["spans"][1].update(pcm_start_byte=63)),
    lambda e: e["spans"][0].update(response_sequence=True), lambda e: e["spans"][1].update(response_sequence=1),
    lambda e: e["spans"][0].update(pcm_end_byte=66), lambda e: e["spans"][1].update(pcm_end_byte=130),
    lambda e: e["spans"][0].update(audio="private"),
    lambda e: e["spans"][0].update(response_sequence=3),
    lambda e: e.update(spans=[{"response_sequence": i+1, "pcm_start_byte": i*2, "pcm_end_byte": i*2+2} for i in range(129)]),
])
def test_malformed_private_or_contradictory_span_metadata_is_rejected(change):
    from api.services.workflow.voice_test_audio_evidence import validate_voice_audio_evidence_report
    from api.services.workflow.voice_test_control import VoiceControlError
    value, report, session = protocol_fixture();change(value["evidence"])
    with pytest.raises(VoiceControlError): validate_voice_audio_evidence_report(value, report, session)


def test_capture_accounts_for_pcm_outside_speech_without_assigning_it_to_a_caller_turn():
    from api.services.workflow.voice_test_audio_evidence import PrivateVoiceAudioEvidence
    capture = PrivateVoiceAudioEvidence()
    capture.record(0, 2, None)
    capture.record(2, 64, 1)
    value = capture.snapshot(pcm_bytes=66, responses=[response(1)], pipeline_stopped=True)
    assert value["complete"] is False
    assert value["unattributed_pcm_bytes"] == 2
    assert value["spans"] == [{"response_sequence": 1, "pcm_start_byte": 2, "pcm_end_byte": 66}]
    value["spans"][0]["pcm_end_byte"] = 4
    assert capture.snapshot(pcm_bytes=66, responses=[response(1)], pipeline_stopped=True)["spans"][0]["pcm_end_byte"] == 66


@pytest.mark.parametrize("bad", ["not_stopped", "open_speech", "empty_response", "missing_response", "offset_missing", "offset_discontinuous", "pcm_mismatch", "zero_pcm"])
def test_missing_pcm_attribution_or_unfinished_pipeline_never_claims_complete(bad):
    from api.services.workflow.voice_test_audio_evidence import PrivateVoiceAudioEvidence
    capture = PrivateVoiceAudioEvidence()
    capture.record(None if bad == "offset_missing" else 2 if bad == "offset_discontinuous" else 0,
                   0 if bad == "zero_pcm" else 64, 1)
    responses = [response(1, closed=bad != "open_speech", has_audio=bad != "empty_response")]
    if bad == "missing_response": responses.append(response(2))
    value = capture.snapshot(pcm_bytes=0 if bad == "zero_pcm" else 66 if bad == "pcm_mismatch" else 64,
                             responses=responses, pipeline_stopped=bad != "not_stopped")
    assert value["complete"] is False


def test_capture_retains_at_most_128_spans_and_marks_overflow_unverified():
    from api.services.workflow.voice_test_audio_evidence import PrivateVoiceAudioEvidence
    capture = PrivateVoiceAudioEvidence()
    for i in range(129): capture.record(i*2, 2, i+1)
    value = capture.snapshot(pcm_bytes=258, responses=[response(i+1) for i in range(128)], pipeline_stopped=True)
    assert len(value["spans"]) == 128
    assert value["unattributed_pcm_bytes"] == 2
    assert value["truncated"] is True and value["complete"] is False


@pytest.mark.asyncio
async def test_job_persists_bound_audio_evidence_and_legacy_status_remains_exact():
    from api.tests.test_voice_tool_trace import completed_trace
    from api.services.workflow.voice_test_job import read_voice_job_audio_evidence, read_voice_job_status
    from api.tests.test_voice_test_control import SECRET
    from api.tests.test_voice_test_job import MemoryRedis
    context, db, control, run, _, report = await completed_trace()
    value = await read_voice_job_audio_evidence(db, control, context, SECRET)
    assert value["audio_sha256"] == report["audio_sha256"]
    assert value["evidence"]["complete"] is True
    assert value["evidence"]["pcm_bytes"] == report["audio_bytes"]-44
    assert len(value["evidence"]["spans"]) == len(report["rehearsal"]["turns"])
    status = await read_voice_job_status(db, control, MemoryRedis(), context, SECRET)
    assert set(status) == {"status", "session_id", "runtime_workflow_run_id", "error_code", "report_status", "rehearsal", "quality_evaluated"}
    assert status["quality_evaluated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["older_writer", "runtime_signature", "audio_hash"])
async def test_audio_readback_rejects_missing_or_forged_native_evidence(bad):
    from api.tests.test_voice_tool_trace import completed_trace
    from api.services.workflow.voice_test_job import read_voice_job_audio_evidence
    from api.tests.test_voice_test_control import SECRET
    from api.services.workflow.voice_test_control import VoiceControlError
    context, db, control, run, _, _ = await completed_trace()
    if bad == "older_writer": del run.annotations["agent_test_voice_audio_evidence"]
    elif bad == "runtime_signature": run.annotations["sysevo_test_sandbox"]["workflow_binding"]["signature"] = "wrong"
    else: run.annotations["agent_test_voice_audio_evidence"]["audio_sha256"] = "f" * 64
    with pytest.raises(ValueError if bad == "runtime_signature" else VoiceControlError):
        await read_voice_job_audio_evidence(db, control, context, SECRET)


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_audio_evidence_http_requires_internal_auth_before_reading(monkeypatch, secret):
    from unittest.mock import AsyncMock
    from api.routes import workflow_voice_test
    from api.tests.test_workflow_voice_test_route import setup
    from api.tests.test_voice_test_control import SESSION, LEASE
    client, _, pool, _ = setup(monkeypatch)
    read = AsyncMock();monkeypatch.setattr(workflow_voice_test, "read_voice_job_audio_evidence", read)
    result = client.post("/workflow/185/test-voice/audio-evidence", json={"session_id": SESSION,"lease_token": LEASE},
                         headers={"x-sysevo-secret":secret} if secret else {})
    assert result.status_code == 403
    read.assert_not_awaited();pool.assert_not_awaited()


def test_audio_evidence_http_uses_authenticated_scope_without_queue_or_capacity(monkeypatch):
    from unittest.mock import AsyncMock
    from api.routes import workflow_voice_test
    from api.tests.test_workflow_voice_test_route import setup
    from api.tests.test_voice_test_control import SESSION, LEASE, SECRET
    client, _, pool, enqueue = setup(monkeypatch)
    read = AsyncMock(return_value={"evidence":{"complete":False}})
    monkeypatch.setattr(workflow_voice_test, "read_voice_job_audio_evidence", read)
    result = client.post("/workflow/185/test-voice/audio-evidence", json={"session_id":SESSION,"lease_token":LEASE},
                         headers={"x-sysevo-secret":SECRET})
    assert result.status_code == 200
    context = read.await_args.args[2]
    assert (context.organization_id,context.requested_by_user_id,context.workflow_id) == (42,9,185)
    assert LEASE not in result.text and SECRET not in result.text
    pool.assert_not_awaited();enqueue.assert_not_awaited()


@pytest.mark.parametrize("extra", [{"organization_id":43},{"url":"https://client.invalid"},{"recipient":"private@example.invalid"}])
def test_audio_evidence_http_cannot_select_another_scope_or_destination(monkeypatch, extra):
    from unittest.mock import AsyncMock
    from api.routes import workflow_voice_test
    from api.tests.test_workflow_voice_test_route import setup
    from api.tests.test_voice_test_control import SESSION, LEASE, SECRET
    client, _, pool, _ = setup(monkeypatch)
    read = AsyncMock();monkeypatch.setattr(workflow_voice_test, "read_voice_job_audio_evidence", read)
    result = client.post("/workflow/185/test-voice/audio-evidence", json={"session_id":SESSION,"lease_token":LEASE,**extra},
                         headers={"x-sysevo-secret":SECRET})
    assert result.status_code == 422
    read.assert_not_awaited();pool.assert_not_awaited()


def test_audio_evidence_http_redacts_unexpected_failures(monkeypatch):
    from unittest.mock import AsyncMock
    from api.routes import workflow_voice_test
    from api.tests.test_workflow_voice_test_route import setup
    from api.tests.test_voice_test_control import SESSION, LEASE, SECRET
    client, _, _, _ = setup(monkeypatch)
    monkeypatch.setattr(workflow_voice_test,"read_voice_job_audio_evidence",AsyncMock(side_effect=RuntimeError("private recording detail")))
    result = client.post("/workflow/185/test-voice/audio-evidence",json={"session_id":SESSION,"lease_token":LEASE},headers={"x-sysevo-secret":SECRET})
    assert result.status_code == 503 and "private recording detail" not in result.text
