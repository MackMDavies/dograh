import copy
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.services.llm_service import LLMService

from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_job import execute_voice_job, read_voice_job_tool_trace, read_voice_job_status
from api.services.workflow.voice_test_tool_trace import (
    PrivateVoiceToolTrace, validate_voice_tool_trace_report,
)
from api.tests.test_voice_test_control import SECRET
from api.tests.test_voice_test_job import MemoryRedis, setup


def service():
    llm = LLMService()
    llm.register_function("book_appointment", AsyncMock())
    llm._run_parallel_function_calls = AsyncMock()
    return llm


def call(name="book_appointment"):
    return FunctionCallFromLLM(name, "private-call-id", {"email": "private@example.invalid"}, None)


@pytest.mark.asyncio
async def test_shared_protocol_fixture_matches_actual_capture_and_native_validator():
    fixture = json.loads((Path(__file__).parent / "fixtures/voiceToolTrace.v1.json").read_text())
    trace = PrivateVoiceToolTrace(["caller-001.wav", "caller-002.wav"])
    llm = service(); trace.attach(llm)
    trace.begin_caller_step("caller-001.wav"); await llm.run_function_calls([call()])
    trace.begin_caller_step("caller-002.wav")
    assert trace.snapshot(pipeline_stopped=True) == fixture["trace"]
    session = SimpleNamespace(scenario_id=fixture["scenario_id"], script=SimpleNamespace(steps=[object(), object()]))
    assert validate_voice_tool_trace_report(fixture, fixture, session) == fixture


def test_a_model_instance_cannot_be_attached_to_two_private_sessions():
    llm = service()
    PrivateVoiceToolTrace(["caller-001.wav"]).attach(llm)
    with pytest.raises(VoiceControlError): PrivateVoiceToolTrace(["caller-001.wav"]).attach(llm)


@pytest.mark.asyncio
async def test_actual_private_pipeline_attaches_capture_before_any_model_or_run_update(monkeypatch):
    from api.services.pipecat import run_pipeline as runtime
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import VoiceTurnObserver
    from api.services.pipecat.audio_config import create_audio_config
    from api.services.configuration import org_provider_resolver
    from api.tests.test_private_voice_provider_pipeline import runtime_fixture, ProviderBoundaryReached
    from api.tests.test_voice_test_providers import SIGNING_SECRET

    run, envelope = runtime_fixture()
    workflow = SimpleNamespace(id=185, organization_id=42, user_id=7, template_context_variables={})
    monkeypatch.setattr(runtime.db_client, "get_workflow", AsyncMock(return_value=workflow))
    forbidden = AsyncMock(side_effect=AssertionError("Must attach before reading owner configuration or updating the run"))
    monkeypatch.setattr(runtime.db_client, "get_user_configurations", forbidden)
    monkeypatch.setattr(runtime.db_client, "update_workflow_run", forbidden)
    for name in ["resolve_org_provider_config", "enrich_config_from_org_connections", "enrich_overrides_with_org_api_keys", "resolve_voice_for_tts"]:
        monkeypatch.setattr(org_provider_resolver, name, forbidden)
    llm = service()
    monkeypatch.setattr(runtime, "create_stt_service", lambda *a, **k: object())
    monkeypatch.setattr(runtime, "create_tts_service", lambda *a, **k: object())
    monkeypatch.setattr(runtime, "create_llm_service", lambda *a, **k: llm)
    transport = SandboxAudioTransport(duration_limit=30)
    observer = VoiceTurnObserver(transport.output())
    observer.tool_trace = SimpleNamespace(attach=Mock(side_effect=ProviderBoundaryReached))
    transport.set_voice_observer(observer)
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SIGNING_SECRET)
    with pytest.raises(ProviderBoundaryReached):
        await runtime._run_pipeline(transport,185,123,7,workflow_run=run,
                                    audio_config=create_audio_config("smallwebrtc"),expected_organization_id=42,
                                    private_voice_providers=envelope)
    observer.tool_trace.attach.assert_called_once_with(llm)
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_model_entry_point_records_before_dispatch_without_copying_values():
    trace = PrivateVoiceToolTrace(["caller-001.wav"])
    llm = service()
    trace.attach(llm)
    trace.begin_caller_step("caller-001.wav")

    async def dispatched(items):
        # No observer drain, background task or event-loop yield is needed.
        assert trace.snapshot(pipeline_stopped=False)["calls"] == [
            {"sequence": 1, "caller_step": 1, "function_name": "book_appointment"}
        ]
        assert items[0].arguments == {"email": "private@example.invalid"}

    llm._run_parallel_function_calls.side_effect = dispatched
    await llm.run_function_calls([call()])
    result = trace.snapshot(pipeline_stopped=True)
    assert result["complete"] is True
    assert "private@example.invalid" not in json.dumps(result)
    assert "private-call-id" not in json.dumps(result)


@pytest.mark.asyncio
async def test_trace_tracks_opening_and_each_saved_caller_step_and_bounds_overflow():
    trace = PrivateVoiceToolTrace(["caller-001.wav", "caller-002.wav"])
    llm = service(); trace.attach(llm)
    await llm.run_function_calls([call()])
    trace.begin_caller_step("caller-001.wav")
    await llm.run_function_calls([call()])
    trace.begin_caller_step("caller-002.wav")
    await llm.run_function_calls([call()] * 63)
    result = trace.snapshot(pipeline_stopped=True)
    assert [row["caller_step"] for row in result["calls"][:3]] == [0, 1, 2]
    assert len(result["calls"]) == 64 and result["truncated"] is True
    assert result["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not_attached", "not_stopped", "steps_missing", "entry_changed", "unknown_name", "step_order"])
async def test_missing_or_changed_capture_never_becomes_complete(bad):
    trace = PrivateVoiceToolTrace(["caller-001.wav"]); llm = service()
    if bad != "not_attached": trace.attach(llm)
    if bad != "steps_missing": trace.begin_caller_step("caller-002.wav" if bad == "step_order" else "caller-001.wav")
    if bad == "entry_changed": llm.run_function_calls = AsyncMock()
    if bad == "unknown_name":
        # An unsupported name is retained as an unidentified attempt, never raw text.
        trace._record([call("private@example.invalid")])
    assert trace.snapshot(pipeline_stopped=bad != "not_stopped")["complete"] is False


async def completed_trace():
    context, db, control, run, pipeline, _, _ = setup()

    async def traced_pipeline(transport, *args):
        llm = service(); transport.voice_observer.tool_trace.attach(llm)
        ready = transport.voice_observer.on_pipeline_started

        async def started():
            original = transport.play_wav

            async def play(encoded):
                await llm.run_function_calls([call()])
                await original(encoded)

            transport.play_wav = play
            await ready()

        transport.voice_observer.on_pipeline_started = started
        await pipeline(transport, *args)

    result = await execute_voice_job(db, control, context, SECRET, pipeline_runner=traced_pipeline)
    assert result["status"] == "completed"
    report = run.annotations["agent_test_voice_result"]
    session = replace(control.get.return_value, status="completed", runtime_workflow_run_id=123,
                      actual_duration_ms=report["actual_duration_ms"], audio_sha256=report["audio_sha256"], audio_bytes=report["audio_bytes"])
    control.get.return_value = session
    return context, db, control, run, session, report


@pytest.mark.asyncio
async def test_job_saves_bound_trace_and_legacy_status_keeps_its_exact_contract():
    context, db, control, run, _, _ = await completed_trace()
    trace = await read_voice_job_tool_trace(db, control, context, SECRET)
    assert trace["trace"]["complete"] is True
    assert trace["trace"]["calls"] == [{"sequence": 1, "caller_step": 1, "function_name": "book_appointment"}]
    assert trace["scenario_id"] == run.annotations["sysevo_test_sandbox"]["scenario_id"]
    status = await read_voice_job_status(db, control, MemoryRedis(), context, SECRET)
    assert set(status) == {"status", "session_id", "runtime_workflow_run_id", "error_code", "report_status", "rehearsal", "quality_evaluated"}
    assert status["report_status"] == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("session_id", "wrong"), ("run_id", "wrong"), ("scenario_id", "wrong"),
    ("workflow_id", True), ("organization_id", 43), ("runtime_workflow_run_id", 124),
    ("configuration_hash", "f" * 64), ("audio_sha256", "f" * 64), ("version", True),
])
async def test_trace_cannot_move_to_another_source_session_scenario_or_recording(field, value):
    _, _, _, run, session, report = await completed_trace()
    trace = {**run.annotations["agent_test_voice_tool_trace"], field: value}
    with pytest.raises(VoiceControlError): validate_voice_tool_trace_report(trace, report, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda t: t.update(truncated=True),
    lambda t: t.update(caller_steps_observed=0),
    lambda t: t["calls"][0].update(sequence=True),
    lambda t: t["calls"][0].update(function_name=None),
    lambda t: t["calls"][0].update(arguments={"email": "private@example.invalid"}),
    lambda t: t["calls"][0].update(caller_step=2),
])
async def test_complete_trace_rejects_unsupported_private_or_contradictory_fields(mutate):
    _, _, _, run, session, report = await completed_trace()
    trace = copy.deepcopy(run.annotations["agent_test_voice_tool_trace"])
    mutate(trace["trace"])
    with pytest.raises(VoiceControlError): validate_voice_tool_trace_report(trace, report, session)


@pytest.mark.asyncio
async def test_an_older_worker_report_without_a_trace_is_unavailable_not_empty():
    context, db, control, run, _, _ = await completed_trace()
    del run.annotations["agent_test_voice_tool_trace"]
    with pytest.raises(VoiceControlError, match="voice_tool_trace_unavailable"):
        await read_voice_job_tool_trace(db, control, context, SECRET)


@pytest.mark.asyncio
async def test_trace_read_rejects_a_forged_runtime_binding():
    context, db, control, run, _, _ = await completed_trace()
    run.annotations["sysevo_test_sandbox"]["workflow_binding"]["signature"] = "wrong"
    with pytest.raises(ValueError): await read_voice_job_tool_trace(db, control, context, SECRET)
