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
    voice = next(item for item in WorkerSettings.functions if getattr(item,"coroutine",None) is agent_test_voice.run_agent_test_voice)
    assert voice.max_tries == 3 and voice.timeout_s == 270 and voice.keep_result_s == 600
