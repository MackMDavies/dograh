import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_job import (
    execute_voice_job,
    read_voice_job_status,
)
from api.services.workflow.voice_test_result import validate_voice_report
from api.tests.test_voice_test_control import SECRET
from api.tests.test_voice_test_job import MemoryRedis, setup


async def completed():
    context, db, control, run, pipeline, _, _ = setup()
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert result["status"] == "completed"
    report = run.annotations["agent_test_voice_result"]
    session = replace(
        control.get.return_value,
        status="completed",
        runtime_workflow_run_id=123,
        actual_duration_ms=report["actual_duration_ms"],
        audio_sha256=report["audio_sha256"],
        audio_bytes=report["audio_bytes"],
    )
    control.get.return_value = session
    return context, db, control, run, session, report


@pytest.mark.asyncio
async def test_completed_status_reconciles_durable_native_evidence_with_sealed_audio():
    context, db, control, _, _, _ = await completed()
    response = await read_voice_job_status(db, control, MemoryRedis(), context, SECRET)
    assert response["status"] == "completed" and response["report_status"] == "verified"
    assert (
        response["quality_evaluated"] is False
        and response["rehearsal"]["quality_evaluated"] is False
    )
    assert len(response["rehearsal"]["turns"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "wrong"),
        ("workflow_id", True),
        ("organization_id", 43),
        ("organization_id", True),
        ("version_kind", "draft"),
        ("runtime_workflow_run_id", 124),
        ("actual_duration_ms", 0),
        ("audio_sha256", "a" * 64),
        ("audio_bytes", 44),
        ("runtime_workflow_hash", "a" * 64),
        ("version", True),
    ],
)
async def test_report_cannot_change_the_identity_source_or_sealed_recording(
    field, value
):
    _, _, _, _, session, report = await completed()
    report = {**report, field: value}
    with pytest.raises(VoiceControlError):
        validate_voice_report(report, session, 42)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["rehearsal"].update(quality_evaluated=True),
        lambda r: r["rehearsal"].update(transcript="private customer text"),
        lambda r: r["rehearsal"].update(turns=[]),
        lambda r: r["rehearsal"]["turns"][0].update(file_name="caller-002.wav"),
        lambda r: r["rehearsal"]["turns"][0].update(response_sequence=True),
        lambda r: r["rehearsal"]["turns"][0].update(
            caller_end_to_first_audio_ms=float("nan")
        ),
        lambda r: r["rehearsal"]["turns"][0].update(caller_end_to_first_audio_ms=True),
        lambda r: r["rehearsal"]["turns"][0].update(recipient="client@example.invalid"),
    ],
)
async def test_report_contains_only_the_exact_saved_exchange_and_finite_timing(mutate):
    _, _, _, _, session, report = await completed()
    report = copy.deepcopy(report)
    mutate(report)
    with pytest.raises(VoiceControlError):
        validate_voice_report(report, session, 42)


@pytest.mark.asyncio
async def test_a_terminal_native_row_without_a_sealed_control_record_is_not_verified():
    _, _, _, _, session, report = await completed()
    with pytest.raises(VoiceControlError, match="voice_report_not_sealed"):
        validate_voice_report(report, replace(session, status="running"), 42)


@pytest.mark.asyncio
async def test_private_runtime_binding_tampering_cannot_be_exposed_as_verified_report():
    context, db, control, run, _, _ = await completed()
    run.annotations["sysevo_test_sandbox"]["workflow_binding"]["signature"] = "bad"
    with pytest.raises(ValueError):
        await read_voice_job_status(db, control, MemoryRedis(), context, SECRET)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_result,expected",
    [
        (
            {"status": "blocked", "code": "voice_worker_capacity_full"},
            "voice_worker_capacity_full",
        ),
        (
            {"code": "private Redis URL with credentials"},
            "voice_worker_result_unverified",
        ),
        ({"code": "voice_" + "x" * 70}, "voice_worker_result_unverified"),
    ],
)
async def test_status_reports_a_blocked_worker_without_echoing_raw_queue_results(
    worker_result, expected
):
    context, db, control, _, _, _, _ = setup()
    with patch("arq.jobs.Job") as job:
        job.return_value.result_info = AsyncMock(
            return_value=SimpleNamespace(success=True, result=worker_result)
        )
        result = await read_voice_job_status(
            db, control, MemoryRedis(), context, SECRET
        )
    assert result["report_status"] == "blocked" and result["error_code"] == expected
    assert result["rehearsal"] is None


@pytest.mark.asyncio
async def test_moved_workflow_does_not_expose_previous_organization_voice_report():
    context, db, control, _, _, _ = await completed()
    context = replace(context, organization_id=43)
    db.get_workflow.return_value = SimpleNamespace(
        id=185, user_id=7, organization_id=43
    )
    with pytest.raises(VoiceControlError, match="voice_report_runtime_invalid"):
        await read_voice_job_status(db, control, MemoryRedis(), context, SECRET)
