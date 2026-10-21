import asyncio
from unittest.mock import AsyncMock

import pytest

from api.services.workflow.voice_test_capacity import (
    WORKER_PROTOCOL_KEY,
    claim_voice_capacity,
    private_voice_capabilities,
    publish_voice_worker_protocol,
    release_voice_capacity,
)
from api.services.workflow.voice_test_control import VoiceControlError
from api.tests.test_voice_test_job import MemoryRedis


@pytest.mark.asyncio
async def test_all_workers_share_three_owner_checked_expiring_slots():
    redis = MemoryRedis()
    slots = await asyncio.gather(*(claim_voice_capacity(redis) for _ in range(3)))
    assert len({slot[0] for slot in slots}) == 3
    assert all(call[1:] == (True, 360) for call in redis.calls)
    with pytest.raises(VoiceControlError, match="voice_worker_capacity_full"):
        await claim_voice_capacity(redis)
    assert not await release_voice_capacity(redis, (slots[0][0], "wrong-owner"))
    assert len(redis.values) == 3
    assert await release_voice_capacity(redis, slots[0])
    assert (await claim_voice_capacity(redis))[0] == slots[0][0]
    assert not await release_voice_capacity(redis, slots[0])


@pytest.mark.asyncio
async def test_redis_failure_never_assumes_free_capacity_or_deletes_an_unowned_slot():
    redis = MemoryRedis()
    redis.set = AsyncMock(side_effect=RuntimeError("private Redis connection details"))
    with pytest.raises(VoiceControlError, match="^voice_worker_capacity_unavailable$"):
        await claim_voice_capacity(redis)
    redis.eval = AsyncMock(side_effect=RuntimeError("private Redis connection details"))
    assert not await release_voice_capacity(redis, ("agent-test-voice-slot:0", "owner"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heartbeat,ready",
    [(None, False), ("3", True), (b"3", True), ("2", False), (b"2", False)],
)
async def test_capabilities_require_fresh_exact_worker_protocol(heartbeat, ready):
    redis = MemoryRedis()
    redis.values[WORKER_PROTOCOL_KEY] = heartbeat
    result = await private_voice_capabilities(redis)
    assert result["worker_ready"] is ready
    assert result["max_concurrent_jobs"] == 3
    assert result["max_duration_seconds"] == 120
    assert result["requires_saved_script"] is True
    assert result["requires_provider_plan"] is True
    assert result["isolated_provider_credentials"] is True
    assert result["protocol_version"] == 3
    assert result["requires_provider_intent"] is True
    assert result["independent_quality_evaluation"] is False


@pytest.mark.asyncio
async def test_capability_read_failure_is_unavailable_without_private_details():
    redis = MemoryRedis()
    redis.get = AsyncMock(side_effect=RuntimeError("private Redis URL"))
    result = await private_voice_capabilities(redis)
    assert result["worker_ready"] is False
    assert "private Redis" not in str(result)


@pytest.mark.asyncio
async def test_worker_publishes_only_the_expiring_isolation_protocol():
    redis = MemoryRedis()
    assert await publish_voice_worker_protocol({"redis": redis})
    assert redis.values == {WORKER_PROTOCOL_KEY: "3"}
    assert redis.calls == [(WORKER_PROTOCOL_KEY, False, 90)]


def test_worker_registration_uses_the_versioned_private_job_name():
    from api.services.workflow.voice_test_job import JOB_NAME
    from api.tasks.arq import WorkerSettings

    names = [
        getattr(function, "name", getattr(function, "__name__", None))
        for function in WorkerSettings.functions
    ]
    assert JOB_NAME == "run_agent_test_voice_v2"
    assert JOB_NAME in names
    assert "run_agent_test_voice" not in names


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", [True, False])
async def test_only_proven_provider_preflight_failure_releases_the_worker_slot(
    monkeypatch, preflight
):
    from api.services.workflow.voice_test_job import (
        VoiceProviderPreflightError,
        voice_job_key,
    )
    from api.tasks import agent_test_voice as worker
    from api.tests.test_voice_test_job import setup

    context, _, _, _, _, _, _ = setup()
    redis = MemoryRedis()
    redis.values[voice_job_key(context.session_id)] = context.encode()
    error = (
        VoiceProviderPreflightError("voice_test_provider_plan_invalid")
        if preflight
        else VoiceControlError("voice_control_unavailable")
    )
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "synthetic-worker-secret")
    monkeypatch.setattr(
        worker, "claim_voice_capacity", AsyncMock(return_value=("slot", "owner"))
    )
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "release_voice_capacity", release)
    monkeypatch.setattr(worker, "execute_voice_job", AsyncMock(side_effect=error))
    result = await worker.run_agent_test_voice({"redis": redis}, context.session_id)
    assert result == {"status": "blocked", "code": error.code}
    if preflight:
        release.assert_awaited_once_with(redis, ("slot", "owner"))
    else:
        release.assert_not_awaited()
