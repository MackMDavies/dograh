import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from api.services.workflow.voice_test_job import VoiceJobContext, voice_job_key
from api.tasks import agent_test_voice
from api.tests.test_voice_test_control import LEASE, SECRET, SESSION
from api.tests.test_voice_test_job import MemoryRedis


def setup(monkeypatch, result=None):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    redis = MemoryRedis()
    redis.values[voice_job_key(SESSION)] = VoiceJobContext(
        SESSION, LEASE, 185, 42, 9
    ).encode()
    execute = AsyncMock(
        return_value=result
        or {"status": "completed", "cleanup_verified": True, "session_id": SESSION}
    )
    monkeypatch.setattr(agent_test_voice, "execute_voice_job", execute)
    return redis, execute


@pytest.mark.asyncio
async def test_task_loads_private_context_and_releases_only_its_completed_slot(
    monkeypatch,
):
    redis, execute = setup(monkeypatch)
    result = await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert result["status"] == "completed"
    assert not any(key.startswith("agent-test-voice-slot:") for key in redis.values)
    assert execute.await_args.args[2].lease_token == LEASE
    assert execute.await_args.args[3] == SECRET
    assert LEASE not in json.dumps(result) and SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_expired_context_never_acquires_capacity_or_starts_work(monkeypatch):
    redis, execute = setup(monkeypatch)
    redis.values.clear()
    assert (await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION))[
        "code"
    ] == "voice_job_context_expired"
    execute.assert_not_awaited()
    assert redis.calls == []


@pytest.mark.asyncio
async def test_full_capacity_does_not_claim_a_paid_execution(monkeypatch):
    redis, execute = setup(monkeypatch)
    for index in range(3):
        redis.values[f"agent-test-voice-slot:{index}"] = "another-owner"
    result = await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert result["code"] == "voice_worker_capacity_full"
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_pipeline_cleanup_quarantines_its_capacity(monkeypatch):
    redis, _ = setup(
        monkeypatch,
        {
            "status": "failed",
            "error_code": "voice_settlement_unverified",
            "cleanup_verified": False,
        },
    )
    await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert sum(key.startswith("agent-test-voice-slot:") for key in redis.values) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_and_keeps_the_slot_until_expiry(monkeypatch):
    redis, execute = setup(monkeypatch)
    execute.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert sum(key.startswith("agent-test-voice-slot:") for key in redis.values) == 1


@pytest.mark.asyncio
async def test_unexpected_worker_errors_have_no_private_detail_in_job_result(
    monkeypatch,
):
    redis, execute = setup(monkeypatch)
    execute.side_effect = RuntimeError(f"private credentials {SECRET} {LEASE}")
    result = await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert result == {"status": "blocked", "code": "voice_job_unavailable"}


def test_worker_registration_includes_the_voice_task():
    from api.tasks.arq import WorkerSettings

    voice = next(
        item
        for item in WorkerSettings.functions
        if getattr(item, "coroutine", None) is agent_test_voice.run_agent_test_voice
    )
    assert (
        voice.max_tries == 3 and voice.timeout_s == 270 and voice.keep_result_s == 600
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_code",
    [
        ("workflow", "voice_workflow_not_found"),
        ("session", "voice_session_expired"),
        ("source", "voice_source_changed"),
        ("fixture", "voice_fixture_invalid"),
        ("provider", "voice_test_provider_plan_invalid"),
        ("unexpected", "voice_preflight_unavailable"),
        ("timeout", "voice_preflight_timeout"),
        ("signer", "voice_control_not_configured"),
    ],
)
async def test_real_preclaim_failures_release_capacity_without_paid_work(
    monkeypatch, failure, expected_code
):
    from dataclasses import replace

    from api.services.workflow import voice_test_job
    from api.services.workflow.voice_test_control import VoiceControlError
    from api.tests.test_voice_test_job import setup as job_setup

    context, db, control, _, _, _, _ = job_setup()
    redis = MemoryRedis()
    redis.values[voice_job_key(SESSION)] = context.encode()
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "" if failure == "signer" else SECRET)
    monkeypatch.setattr(agent_test_voice, "db_client", db)
    monkeypatch.setattr(agent_test_voice, "VoiceControlClient", lambda *_: control)
    if failure == "workflow":
        db.get_workflow.return_value = None
    elif failure == "session":
        control.get.side_effect = VoiceControlError("voice_session_expired")
    elif failure == "source":
        db.get_workflow_versions.return_value[0].workflow_json = {
            "nodes": [{"id": "changed"}]
        }
    elif failure == "fixture":
        control.fixture.side_effect = VoiceControlError("voice_fixture_invalid")
    elif failure == "provider":
        control.get.return_value = replace(control.get.return_value, provider_plan=None)
    elif failure == "unexpected":
        control.fixture.side_effect = RuntimeError(f"private {SECRET} {LEASE}")
    elif failure == "timeout":
        monkeypatch.setattr(voice_test_job, "PREFLIGHT_TIMEOUT_SECONDS", 0.01)

        async def stalled(*_):
            await asyncio.Event().wait()

        control.fixture.side_effect = stalled

    result = await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert result == {"status": "blocked", "code": expected_code}
    assert redis.values == {voice_job_key(SESSION): context.encode()}
    control.claim.assert_not_awaited()
    control.attach.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preflight", "claim", "after_claim"])
async def test_real_cancellation_only_releases_capacity_before_claim(
    monkeypatch, phase
):
    from api.tests.test_voice_test_job import setup as job_setup

    context, db, control, _, _, _, _ = job_setup()
    redis = MemoryRedis()
    redis.values[voice_job_key(SESSION)] = context.encode()
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    monkeypatch.setattr(agent_test_voice, "db_client", db)
    monkeypatch.setattr(agent_test_voice, "VoiceControlClient", lambda *_: control)
    entered = asyncio.Event()

    async def stalled(*_, **__):
        entered.set()
        await asyncio.Event().wait()

    boundary = {
        "preflight": control.fixture,
        "claim": control.claim,
        "after_claim": db.create_workflow_run,
    }[phase]
    boundary.side_effect = stalled
    task = asyncio.create_task(
        agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    )
    async with asyncio.timeout(5):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    slots = [key for key in redis.values if key.startswith("agent-test-voice-slot:")]
    assert len(slots) == (0 if phase == "preflight" else 1)
    if phase == "preflight":
        control.claim.assert_not_awaited()
    else:
        control.claim.assert_awaited_once()
    control.attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_uncertain_claim_keeps_capacity_without_starting_runtime(
    monkeypatch,
):
    from api.services.workflow.voice_test_control import VoiceControlError
    from api.tests.test_voice_test_job import setup as job_setup

    context, db, control, _, _, _, _ = job_setup()
    redis = MemoryRedis()
    redis.values[voice_job_key(SESSION)] = context.encode()
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    monkeypatch.setattr(agent_test_voice, "db_client", db)
    monkeypatch.setattr(agent_test_voice, "VoiceControlClient", lambda *_: control)
    control.claim.side_effect = VoiceControlError("voice_control_unavailable")
    result = await agent_test_voice.run_agent_test_voice({"redis": redis}, SESSION)
    assert result == {"status": "blocked", "code": "voice_control_unavailable"}
    assert sum(key.startswith("agent-test-voice-slot:") for key in redis.values) == 1
    control.claim.assert_awaited_once()
    db.create_workflow_run.assert_not_awaited()
