from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from api.services.workflow.test_sandbox_binding import (
    make_sandbox_workflow_binding,
    validate_sandbox_pipeline_binding,
)

SECRET = "synthetic-voice-test-secret"


def signed_voice_run():
    definition = SimpleNamespace(
        workflow_id=42,
        version_number=2,
        status="draft",
        workflow_json={"nodes": []},
        workflow_configurations={},
        template_context_variables={},
    )
    marker = {
        "enabled": True,
        "agent_test_run_id": "11111111-1111-4111-8111-111111111111",
        "scenario_id": "audio_fixture",
    }
    marker["workflow_binding"] = make_sandbox_workflow_binding(
        definition,
        workflow_id=42,
        workflow_run_id=7,
        run_id=marker["agent_test_run_id"],
        scenario_id=marker["scenario_id"],
        signing_secret=SECRET,
    )
    return SimpleNamespace(
        id=7,
        workflow_id=42,
        definition=definition,
        is_completed=False,
        mode="smallwebrtc",
        initial_context={},
        annotations={"sysevo_test_sandbox": marker},
    )


def test_voice_pipeline_requires_exact_bound_identity_and_configuration():
    run = signed_voice_run()
    assert validate_sandbox_pipeline_binding(run, 42, 7, SECRET) is True
    for workflow_id, run_id in [(43, 7), (42, 8)]:
        with pytest.raises(ValueError, match="identity"):
            validate_sandbox_pipeline_binding(run, workflow_id, run_id, SECRET)
    run.definition.workflow_configurations["voice"] = "mutated"
    with pytest.raises(ValueError, match="configuration changed"):
        validate_sandbox_pipeline_binding(run, 42, 7, SECRET)


def test_unbound_or_unsigned_markers_cannot_start_a_voice_pipeline():
    run = signed_voice_run()
    with pytest.raises(ValueError):
        validate_sandbox_pipeline_binding(run, 42, 7, "wrong-secret")
    del run.annotations["sysevo_test_sandbox"]["workflow_binding"]
    with pytest.raises(ValueError, match="no server-owned"):
        validate_sandbox_pipeline_binding(run, 42, 7, SECRET)
    assert (
        validate_sandbox_pipeline_binding(SimpleNamespace(annotations={}), 42, 7, "")
        is False
    )


@pytest.mark.parametrize("mode", ["textchat", "twilio", "unknown", None])
def test_text_sessions_and_unverified_carrier_modes_cannot_be_repurposed_for_pcm(mode):
    run = signed_voice_run()
    run.mode = mode
    with pytest.raises(ValueError, match="dedicated WebRTC"):
        validate_sandbox_pipeline_binding(run, 42, 7, SECRET)


@pytest.mark.parametrize("complete_identity", [False, True])
def test_integration_factories_never_receive_a_sandbox_voice_run(
    monkeypatch, complete_identity
):
    from api.services.integrations import registry

    run = (
        signed_voice_run()
        if complete_identity
        else SimpleNamespace(
            annotations={"sysevo_test_sandbox": {"enabled": True}},
        )
    )
    load = Mock(
        side_effect=AssertionError("Private test loaded client integration packages")
    )
    monkeypatch.setattr(registry, "_ensure_loaded", load)
    assert registry.create_runtime_sessions(SimpleNamespace(workflow_run=run)) == []
    load.assert_not_called()


def test_normal_voice_runs_keep_registered_integration_collectors(monkeypatch):
    from api.services.integrations import registry

    context = SimpleNamespace(workflow_run=SimpleNamespace(annotations={}))
    factory = Mock(return_value=["normal-session"])
    monkeypatch.setattr(registry, "_ensure_loaded", Mock())
    monkeypatch.setattr(
        registry,
        "all_packages",
        lambda: [SimpleNamespace(create_runtime_sessions=factory)],
    )
    assert registry.create_runtime_sessions(context) == ["normal-session"]
    factory.assert_called_once_with(context)


@pytest.mark.asyncio
async def test_voice_binding_rejection_precedes_workflow_or_provider_access(
    monkeypatch,
):
    from api.services.pipecat import run_pipeline

    run = signed_voice_run()
    run.definition.workflow_json["mutated"] = True
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    monkeypatch.setattr(
        run_pipeline.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)
    )
    metadata_read = AsyncMock(
        side_effect=AssertionError("Read workflow before rejecting binding")
    )
    monkeypatch.setattr(run_pipeline.db_client, "get_workflow_by_id", metadata_read)
    factories = [Mock() for _ in range(4)]
    for name, factory in zip(
        [
            "create_stt_service",
            "create_llm_service",
            "create_tts_service",
            "create_realtime_llm_service",
        ],
        factories,
    ):
        monkeypatch.setattr(run_pipeline, name, factory)
    with pytest.raises(HTTPException) as raised:
        await run_pipeline._run_pipeline(None, 42, 7, 1)
    assert raised.value.status_code == 409
    metadata_read.assert_not_awaited()
    for factory in factories:
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_private_pcm_transport_cannot_run_as_an_ordinary_client_call(monkeypatch):
    from api.services.pipecat import run_pipeline
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport

    run = signed_voice_run()
    run.annotations = {}
    monkeypatch.setattr(
        run_pipeline.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)
    )
    metadata = AsyncMock()
    monkeypatch.setattr(run_pipeline.db_client, "get_workflow_by_id", metadata)
    with pytest.raises(HTTPException) as raised:
        await run_pipeline._run_pipeline(
            SandboxAudioTransport(duration_limit=1), 42, 7, 1
        )
    assert raised.value.status_code == 409
    metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [False, True])
async def test_sandbox_voice_does_not_emit_client_call_analytics(monkeypatch, sandbox):
    from api.services.pipecat import event_handlers

    run = (
        signed_voice_run()
        if sandbox
        else SimpleNamespace(
            workflow_id=42,
            mode="smallwebrtc",
            initial_context={},
            annotations={},
        )
    )
    monkeypatch.setattr(
        event_handlers.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)
    )
    capture = Mock()
    monkeypatch.setattr(event_handlers, "capture_event", capture)
    await event_handlers._capture_call_event(7, "test-user", "call_started")
    assert capture.call_count == (0 if sandbox else 1)
