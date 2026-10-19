import asyncio
import io
import struct
import wave
from unittest.mock import AsyncMock, Mock

import pytest

from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.run_pipeline import _run_pipeline
from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
from api.services.workflow.test_sandbox_binding import make_sandbox_workflow_binding
from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline import WORKFLOW_DEFINITION


@pytest.mark.asyncio
async def test_attested_audio_pipeline_finishes_without_tenant_metadata_or_supervisor(
    db_session,
    async_session,
    monkeypatch,
):
    run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=WORKFLOW_DEFINITION,
        name_prefix="Private Voice Test",
        provider_id_suffix="private-voice-test",
    )
    run = await db_session.get_workflow_run_by_id(run.id)
    original_dispositions = list(workflow.call_disposition_codes or [])
    secret = "synthetic-private-voice-secret"
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", secret)
    marker = {
        "enabled": True,
        "agent_test_run_id": "11111111-1111-4111-8111-111111111111",
        "scenario_id": "private_audio",
    }
    marker["workflow_binding"] = make_sandbox_workflow_binding(
        run.definition,
        workflow_id=workflow.id,
        workflow_run_id=run.id,
        run_id=marker["agent_test_run_id"],
        scenario_id=marker["scenario_id"],
        signing_secret=secret,
    )
    await db_session.update_workflow_run(
        run.id,
        annotations={"sysevo_test_sandbox": marker},
        gathered_context={"mapped_call_disposition": "private-test-only"},
    )
    metadata_write = AsyncMock()
    monkeypatch.setattr(db_session, "add_call_disposition_code", metadata_write)
    supervisor = Mock()
    monkeypatch.setattr(
        "api.services.supervisor.live_control.SupervisorControl", supervisor
    )
    captured = []
    transport = SandboxAudioTransport(duration_limit=30)
    with patch_run_pipeline_externals(captured):
        running = asyncio.create_task(
            _run_pipeline(
                transport,
                workflow.id,
                run.id,
                user.id,
                audio_config=create_audio_config("smallwebrtc"),
                user_provider_id=user.provider_id,
            )
        )
        try:
            for _ in range(100):
                if captured or running.done():
                    break
                await asyncio.sleep(0.05)
            if running.done():
                running.result()
            assert captured, "Private voice pipeline never created its task"
            await asyncio.wait_for(captured[0]._pipeline_start_event.wait(), timeout=5)
            caller_audio = io.BytesIO()
            with wave.open(caller_audio, "wb") as fixture:
                fixture.setnchannels(1)
                fixture.setsampwidth(2)
                fixture.setframerate(16000)
                fixture.writeframes(struct.pack("<h", 1000) * 1280)
            await transport.play_wav(caller_audio.getvalue())
            await captured[0].cancel()
            await asyncio.wait_for(running, timeout=10)
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    persisted = await db_session.get_workflow_run_by_id(run.id)
    assert persisted.is_completed is True
    assert (
        persisted.annotations["sysevo_test_sandbox"]["workflow_binding"]
        == marker["workflow_binding"]
    )
    assert persisted.gathered_context["mapped_call_disposition"] == "completed"
    reloaded_workflow = await db_session.get_workflow(
        workflow.id, organization_id=workflow.organization_id
    )
    assert list(reloaded_workflow.call_disposition_codes or []) == original_dispositions
    metadata_write.assert_not_awaited()
    supervisor.assert_not_called()
