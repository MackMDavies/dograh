import asyncio
from unittest.mock import AsyncMock

import pytest

from api.services.workflow.voice_test_capacity import (
    claim_voice_capacity,
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
