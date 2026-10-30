"""Real local Redis proofs; only UUID-scoped test keys are ever modified."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from api.services.campaign.errors import PhoneNumberDailyLimitError
from api.services.campaign.rate_limiter import RateLimiter, rate_limiter
from api.services.telephony.number_policy import NumberPolicy, reserve_daily_attempt

NY, LA = '+12125550101', '+13105550101'


@pytest.fixture
async def budget(monkeypatch):
    limiter = RateLimiter()
    redis = await limiter._get_redis()
    org = uuid.uuid4().int % 1_000_000_000
    cfg = uuid.uuid4().int % 1_000_000_000
    policy = NumberPolicy(org, cfg, NY, 50)
    other_key = 'voice-number-proof:' + uuid.uuid4().hex
    monkeypatch.setattr(rate_limiter, '_get_redis', AsyncMock(return_value=redis))
    try:
        yield limiter, redis, org, cfg, policy, other_key
    finally:
        await redis.delete(policy.day_key(), other_key,
            limiter._from_number_pool_key(org, cfg), limiter._from_number_leases_key(org, cfg))
        await limiter.close()


async def test_simultaneous_attempts_never_exceed_the_saved_daily_cap(budget):
    _, redis, _, _, policy, _ = budget
    results = await asyncio.gather(*(reserve_daily_attempt(policy) for _ in range(100)))
    assert results.count(True) == 50
    assert int(await redis.get(policy.day_key())) == 50


async def test_pool_prefers_matching_area_but_respects_concurrent_capacity(budget):
    limiter, _, org, cfg, policy, other = budget
    await limiter.initialize_from_number_pool(org, [NY, LA], telephony_configuration_id=cfg)
    preferences = {NY: {'key': policy.day_key(), 'limit': 50, 'preferred': True},
                   LA: {'key': other, 'limit': 50, 'preferred': False}}
    first = await limiter.acquire_from_number(org, cfg, calls_per_number=1, number_preferences=preferences)
    second = await limiter.acquire_from_number(org, cfg, calls_per_number=1, number_preferences=preferences)
    assert first.number == NY and second.number == LA


async def test_capped_local_number_falls_back_to_an_available_owned_number(budget):
    limiter, redis, org, cfg, policy, other = budget
    await limiter.initialize_from_number_pool(org, [NY, LA], telephony_configuration_id=cfg)
    await redis.set(policy.day_key(), 50)
    preferences = {NY: {'key': policy.day_key(), 'limit': 50, 'preferred': True},
                   LA: {'key': other, 'limit': 50, 'preferred': False}}
    lease = await limiter.acquire_from_number(org, cfg, number_preferences=preferences)
    assert lease.number == LA


async def test_all_daily_caps_exhausted_defer_immediately_without_taking_a_lease(budget):
    limiter, redis, org, cfg, policy, other = budget
    await limiter.initialize_from_number_pool(org, [NY, LA], telephony_configuration_id=cfg)
    await redis.set(policy.day_key(), 50)
    await redis.set(other, 50)
    preferences = {NY: {'key': policy.day_key(), 'limit': 50}, LA: {'key': other, 'limit': 50}}
    with pytest.raises(PhoneNumberDailyLimitError):
        await limiter.acquire_from_number(org, cfg, number_preferences=preferences)
    assert await redis.zcard(limiter._from_number_leases_key(org, cfg)) == 0


async def test_local_presence_never_escapes_a_pinned_caller_id_pool(budget):
    limiter, _, org, cfg, policy, other = budget
    await limiter.initialize_from_number_pool(org, [NY], telephony_configuration_id=cfg)
    preferences = {NY: {'key': policy.day_key(), 'limit': 50, 'preferred': False},
                   LA: {'key': other, 'limit': 50, 'preferred': True}}
    lease = await limiter.acquire_from_number(org, cfg, number_preferences=preferences)
    assert lease.number == NY
