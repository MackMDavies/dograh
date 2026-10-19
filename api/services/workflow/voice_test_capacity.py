"""Three expiring, owner-checked slots across all native voice-test workers."""

import asyncio
from uuid import uuid4

from api.services.workflow.voice_test_control import VoiceControlError

SLOT_TTL_SECONDS = 360
WORKER_PROTOCOL_KEY = "agent-test-voice-worker-protocol:v1"
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


async def publish_voice_worker_protocol(ctx):
    try:
        async with asyncio.timeout(5):
            await ctx["redis"].set(WORKER_PROTOCOL_KEY, "1", ex=90)
    except Exception:  # noqa: BLE001 - keep private Redis errors out of worker logs
        # Admission stays blocked until a worker can refresh this capability.
        return False
    return True


async def claim_voice_capacity(redis):
    token = str(uuid4())
    try:
        async with asyncio.timeout(5):
            for index in range(3):
                key = f"agent-test-voice-slot:{index}"
                if await redis.set(key, token, nx=True, ex=SLOT_TTL_SECONDS):
                    return key, token
    except Exception:  # noqa: BLE001 - capacity is closed on any uncertain Redis outcome
        raise VoiceControlError("voice_worker_capacity_unavailable") from None
    raise VoiceControlError("voice_worker_capacity_full")


async def release_voice_capacity(redis, slot):
    try:
        async with asyncio.timeout(5):
            return await redis.eval(RELEASE, 1, slot[0], slot[1]) == 1
    except Exception:  # noqa: BLE001 - quarantine on any uncertain release
        # An uncertain release quarantines the slot until expiry. Never delete
        # a key without its token or open capacity by assuming Redis succeeded.
        return False
