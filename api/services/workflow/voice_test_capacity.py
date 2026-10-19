"""Three expiring, owner-checked slots across all native voice-test workers."""

import asyncio
from uuid import uuid4

from api.services.workflow.voice_test_control import VoiceControlError

MAX_VOICE_JOBS = 3
SLOT_TTL_SECONDS = 360
WORKER_PROTOCOL_VERSION = "2"
WORKER_PROTOCOL_KEY = "agent-test-voice-worker-protocol:v2"
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


async def publish_voice_worker_protocol(ctx):
    try:
        async with asyncio.timeout(5):
            await ctx["redis"].set(WORKER_PROTOCOL_KEY, WORKER_PROTOCOL_VERSION, ex=90)
    except Exception:  # noqa: BLE001 - keep private Redis errors out of worker logs
        # Admission stays blocked until a worker can refresh this capability.
        return False
    return True


async def claim_voice_capacity(redis):
    token = str(uuid4())
    try:
        async with asyncio.timeout(5):
            for index in range(MAX_VOICE_JOBS):
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


async def private_voice_capabilities(redis):
    """Describe transport support and actual worker liveness, never voice quality."""
    try:
        async with asyncio.timeout(5):
            ready = await redis.get(WORKER_PROTOCOL_KEY) in (
                WORKER_PROTOCOL_VERSION,
                WORKER_PROTOCOL_VERSION.encode(),
            )
    except Exception:  # noqa: BLE001 - availability probes must not expose Redis details
        ready = False
    return {
        "protocol_version": 2,
        "worker_ready": ready,
        "max_concurrent_jobs": MAX_VOICE_JOBS,
        "max_duration_seconds": 120,
        "requires_saved_script": True,
        "requires_provider_plan": True,
        "isolated_provider_credentials": True,
        "independent_quality_evaluation": False,
        "measurement_scope": "saved caller PCM to native output speech segments; excludes carrier and network latency",
    }
