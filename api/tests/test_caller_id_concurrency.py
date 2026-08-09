"""One caller ID must be able to carry more than one simultaneous call.

The from_number pool was strictly exclusive — one live call per configured
number — and the campaign validator took ``min(org_limit, phone_number_count)``.
An organisation with a single DID was therefore pinned to a concurrency of 1
regardless of its plan, and told to buy more numbers.

These tests pin the corrected behaviour:
  * with no per-CLI cap (the default), a single number serves unlimited
    simultaneous calls and never exhausts the pool;
  * caller-ID supply no longer lowers the effective campaign limit;
  * an explicitly configured cap is still enforced, and calls spread across
    numbers rather than piling onto one;
  * leases release individually, so ending one call does not free another's
    capacity.
"""

import os
import uuid

import pytest

from api.services.campaign.caller_id_capacity import effective_concurrency_limit
from api.services.campaign.rate_limiter import RateLimiter

requires_redis = pytest.mark.skipif(
    "REDIS_URL" not in os.environ,
    reason="Requires Redis (set REDIS_URL via .env.test)",
)


def _unique_id() -> int:
    return uuid.uuid4().int % 10_000_000


@pytest.fixture
async def isolated_rate_limiter():
    """A RateLimiter against the configured Redis, cleaning up its own keys."""
    rl = RateLimiter()
    redis_client = await rl._get_redis()
    created_keys: list[str] = []

    original_eval = redis_client.eval
    original_zadd = redis_client.zadd

    async def tracking_eval(script, numkeys, *args, **kwargs):
        for i in range(numkeys):
            created_keys.append(args[i])
        return await original_eval(script, numkeys, *args, **kwargs)

    async def tracking_zadd(name, *args, **kwargs):
        created_keys.append(name)
        return await original_zadd(name, *args, **kwargs)

    redis_client.eval = tracking_eval  # type: ignore[assignment]
    redis_client.zadd = tracking_zadd  # type: ignore[assignment]

    yield rl

    redis_client.eval = original_eval  # type: ignore[assignment]
    redis_client.zadd = original_zadd  # type: ignore[assignment]
    if created_keys:
        await redis_client.delete(*set(created_keys))
    await rl.close()


# ---------------------------------------------------------------------------
# The effective-limit calculation (pure, no Redis)
# ---------------------------------------------------------------------------


class TestEffectiveConcurrencyLimit:
    def test_single_number_does_not_cap_concurrency_by_default(self):
        """The regression this whole change exists for: 1 number, org allows 10."""
        assert (
            effective_concurrency_limit(
                org_limit=10, from_numbers_count=1, calls_per_number=None
            )
            == 10
        ), "one configured caller ID must not pin the org to a concurrency of 1"

    def test_zero_numbers_falls_back_to_org_limit(self):
        assert (
            effective_concurrency_limit(
                org_limit=20, from_numbers_count=0, calls_per_number=None
            )
            == 20
        )

    def test_org_limit_still_governs(self):
        assert (
            effective_concurrency_limit(
                org_limit=5, from_numbers_count=50, calls_per_number=None
            )
            == 5
        )

    def test_explicit_cap_multiplies_across_numbers(self):
        assert (
            effective_concurrency_limit(
                org_limit=20, from_numbers_count=3, calls_per_number=4
            )
            == 12
        )

    def test_explicit_cap_never_exceeds_org_limit(self):
        assert (
            effective_concurrency_limit(
                org_limit=8, from_numbers_count=3, calls_per_number=4
            )
            == 8
        )

    def test_cap_with_unknown_number_count_does_not_zero_the_limit(self):
        """A failed number lookup must not compute a ceiling of 0 calls."""
        assert (
            effective_concurrency_limit(
                org_limit=10, from_numbers_count=0, calls_per_number=2
            )
            == 10
        )


# ---------------------------------------------------------------------------
# The Redis-backed pool
# ---------------------------------------------------------------------------


class TestUncappedCallerIdSharing:
    @requires_redis
    @pytest.mark.asyncio
    async def test_one_number_serves_many_simultaneous_calls(
        self, isolated_rate_limiter
    ):
        rl = isolated_rate_limiter
        org_id = _unique_id()
        config_id = _unique_id()
        number = "+15551110001"

        await rl.initialize_from_number_pool(
            org_id, [number], telephony_configuration_id=config_id
        )

        # Twenty concurrent calls, nothing released in between.
        leases = []
        for _ in range(20):
            lease = await rl.acquire_from_number(
                org_id, telephony_configuration_id=config_id
            )
            assert lease is not None, (
                "a single caller ID must serve unlimited simultaneous calls "
                f"by default; pool reported exhausted after {len(leases)}"
            )
            assert lease.number == number
            leases.append(lease)

        assert len({lease.lease_id for lease in leases}) == 20, (
            "each simultaneous call must get its own lease id"
        )
        assert (
            await rl.count_from_number_leases(
                org_id, config_id, from_number=number
            )
            == 20
        )

    @requires_redis
    @pytest.mark.asyncio
    async def test_releasing_one_call_leaves_the_others_holding(
        self, isolated_rate_limiter
    ):
        rl = isolated_rate_limiter
        org_id = _unique_id()
        config_id = _unique_id()
        number = "+15551110001"

        await rl.initialize_from_number_pool(
            org_id, [number], telephony_configuration_id=config_id
        )

        first = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id
        )
        second = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id
        )
        third = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id
        )

        released = await rl.release_from_number(
            org_id,
            second.number,
            telephony_configuration_id=config_id,
            lease_id=second.lease_id,
        )
        assert released is True
        assert (
            await rl.count_from_number_leases(org_id, config_id, from_number=number)
            == 2
        ), "releasing one call must not drop the other two calls' leases"

        # A duplicate release (webhooks retry) must be a no-op, not a steal of
        # another live call's lease.
        assert (
            await rl.release_from_number(
                org_id,
                second.number,
                telephony_configuration_id=config_id,
                lease_id=second.lease_id,
            )
            is False
        ), "releasing an already-released lease must report no release"

        assert (
            await rl.count_from_number_leases(org_id, config_id, from_number=number)
            == 2
        ), "a duplicate release must not free a different call's capacity"

        for lease in (first, third):
            await rl.release_from_number(
                org_id,
                lease.number,
                telephony_configuration_id=config_id,
                lease_id=lease.lease_id,
            )
        assert (
            await rl.count_from_number_leases(org_id, config_id, from_number=number)
            == 0
        )

    @requires_redis
    @pytest.mark.asyncio
    async def test_empty_pool_still_returns_none(self, isolated_rate_limiter):
        """No configured numbers is a real failure — don't invent a caller ID."""
        rl = isolated_rate_limiter
        lease = await rl.acquire_from_number(
            _unique_id(), telephony_configuration_id=_unique_id()
        )
        assert lease is None


class TestConfiguredPerNumberCap:
    @requires_redis
    @pytest.mark.asyncio
    async def test_cap_is_enforced_per_number(self, isolated_rate_limiter):
        rl = isolated_rate_limiter
        org_id = _unique_id()
        config_id = _unique_id()
        numbers = ["+15551110001", "+15551110002"]

        await rl.initialize_from_number_pool(
            org_id, numbers, telephony_configuration_id=config_id
        )

        # 2 numbers x cap of 2 = 4 concurrent calls, then exhaustion.
        leases = []
        for _ in range(4):
            lease = await rl.acquire_from_number(
                org_id, telephony_configuration_id=config_id, calls_per_number=2
            )
            assert lease is not None
            leases.append(lease)

        assert (
            await rl.acquire_from_number(
                org_id, telephony_configuration_id=config_id, calls_per_number=2
            )
            is None
        ), "the 5th call must be refused once every number is at its cap"

        # And the load must be spread, not piled onto one number.
        per_number = {
            number: await rl.count_from_number_leases(
                org_id, config_id, from_number=number
            )
            for number in numbers
        }
        assert per_number == {numbers[0]: 2, numbers[1]: 2}, (
            f"calls should spread evenly across caller IDs, got {per_number}"
        )

    @requires_redis
    @pytest.mark.asyncio
    async def test_release_frees_capacity_under_a_cap(self, isolated_rate_limiter):
        rl = isolated_rate_limiter
        org_id = _unique_id()
        config_id = _unique_id()
        number = "+15551110001"

        await rl.initialize_from_number_pool(
            org_id, [number], telephony_configuration_id=config_id
        )

        first = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id, calls_per_number=1
        )
        assert first is not None
        assert (
            await rl.acquire_from_number(
                org_id, telephony_configuration_id=config_id, calls_per_number=1
            )
            is None
        )

        await rl.release_from_number(
            org_id,
            first.number,
            telephony_configuration_id=config_id,
            lease_id=first.lease_id,
        )

        again = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id, calls_per_number=1
        )
        assert again is not None, "capacity must return once the call ends"

    @requires_redis
    @pytest.mark.asyncio
    async def test_stale_leases_are_reclaimed(self, isolated_rate_limiter):
        """A call that never released must not hold a caller ID forever."""
        rl = isolated_rate_limiter
        org_id = _unique_id()
        config_id = _unique_id()
        number = "+15551110001"

        await rl.initialize_from_number_pool(
            org_id, [number], telephony_configuration_id=config_id
        )

        held = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id, calls_per_number=1
        )
        assert held is not None
        assert (
            await rl.acquire_from_number(
                org_id, telephony_configuration_id=config_id, calls_per_number=1
            )
            is None
        )

        # Simulate the lease ageing past the stale window.
        rl.stale_call_timeout = 0
        reclaimed = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id, calls_per_number=1
        )
        assert reclaimed is not None, (
            "leases older than stale_call_timeout must be reclaimed so an "
            "abandoned call cannot permanently consume a caller ID"
        )

    @requires_redis
    @pytest.mark.asyncio
    async def test_release_without_lease_id_still_frees_capacity(
        self, isolated_rate_limiter
    ):
        """Runs dispatched before leases existed have no lease_id in Redis."""
        rl = isolated_rate_limiter
        org_id = _unique_id()
        config_id = _unique_id()
        number = "+15551110001"

        await rl.initialize_from_number_pool(
            org_id, [number], telephony_configuration_id=config_id
        )
        acquired = await rl.acquire_from_number(
            org_id, telephony_configuration_id=config_id, calls_per_number=1
        )
        assert acquired is not None

        released = await rl.release_from_number(
            org_id, number, telephony_configuration_id=config_id, lease_id=None
        )
        assert released is True
        assert (
            await rl.count_from_number_leases(org_id, config_id, from_number=number)
            == 0
        )
