import time
import uuid
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL


@dataclass(frozen=True)
class FromNumberLease:
    """One live call's hold on a caller ID.

    A caller ID may back several simultaneous calls, so the number alone no
    longer identifies what to release — the lease id does.
    """

    number: str
    lease_id: str


class RateLimiter:
    """Sliding window rate limiter to enforce strict per-second limits and concurrent call limits"""

    def __init__(self):
        self.redis_client: Optional[aioredis.Redis] = None
        self.stale_call_timeout = 1200  # 20 minutes in seconds
        # Leases outlive the stale sweep so a busy pool's key is never dropped
        # mid-call; each acquire refreshes it.
        self._lease_ttl = 3600

    async def _get_redis(self) -> aioredis.Redis:
        """Get or create Redis connection"""
        if self.redis_client is None:
            self.redis_client = await aioredis.from_url(
                REDIS_URL, decode_responses=True
            )
        return self.redis_client

    async def acquire_token(self, organization_id: int, rate_limit: int = 1) -> bool:
        """
        Enforces strict rate limit: max N calls per rolling second window
        Returns True if allowed, False if rate limited
        """
        redis_client = await self._get_redis()

        key = f"rate_limit:{organization_id}"
        now = time.time()
        window_start = now - 1.0  # 1 second sliding window

        # Lua script for atomic sliding window operation
        lua_script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local window_start = tonumber(ARGV[2])
        local max_requests = tonumber(ARGV[3])
        
        -- Remove timestamps older than window
        redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
        
        -- Count requests in current window
        local current_requests = redis.call('ZCARD', key)
        
        if current_requests < max_requests then
            -- Add current timestamp
            redis.call('ZADD', key, now, now)
            redis.call('EXPIRE', key, 2)  -- Expire after 2 seconds
            return 1
        else
            return 0
        end
        """

        try:
            result = await redis_client.eval(
                lua_script, 1, key, now, window_start, rate_limit
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Rate limiter error: {e}")
            # On error, be conservative and deny
            return False

    async def get_next_available_slot(
        self, organization_id: int, rate_limit: int = 1
    ) -> float:
        """
        Returns seconds until next available slot
        Useful for implementing retry with backoff
        """
        redis_client = await self._get_redis()

        key = f"rate_limit:{organization_id}"

        try:
            # Get oldest timestamp in current window
            oldest = await redis_client.zrange(key, 0, 0, withscores=True)
            if not oldest:
                return 0.0  # Can call immediately

            oldest_time = oldest[0][1]
            next_available = oldest_time + 1.0  # 1 second after oldest
            wait_time = max(0, next_available - time.time())

            return wait_time
        except Exception as e:
            logger.error(f"Rate limiter get_next_available_slot error: {e}")
            return 1.0  # Default wait time on error

    async def try_acquire_concurrent_slot(
        self, organization_id: int, max_concurrent: int = 20
    ) -> Optional[str]:
        """
        Try to acquire a concurrent call slot.
        Returns a unique slot_id if successful, None if limit reached.
        """
        redis_client = await self._get_redis()

        concurrent_key = f"concurrent_calls:{organization_id}"
        now = time.time()
        stale_cutoff = now - self.stale_call_timeout

        # Lua script for atomic operation
        lua_script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local max_concurrent = tonumber(ARGV[2])
        local stale_cutoff = tonumber(ARGV[3])
        local slot_id = ARGV[4]
        
        -- Remove stale entries (older than 30 minutes)
        redis.call('ZREMRANGEBYSCORE', key, 0, stale_cutoff)
        
        -- Get current count
        local current_count = redis.call('ZCARD', key)
        
        if current_count < max_concurrent then
            -- Add new slot
            redis.call('ZADD', key, now, slot_id)
            redis.call('EXPIRE', key, 3600)  -- Expire after 1 hour
            return slot_id
        else
            return nil
        end
        """

        # Generate unique slot ID (timestamp + random component)
        slot_id = f"{int(now * 1000)}_{uuid.uuid4().hex[:8]}"

        try:
            result = await redis_client.eval(
                lua_script,
                1,
                concurrent_key,
                now,
                max_concurrent,
                stale_cutoff,
                slot_id,
            )
            return result
        except Exception as e:
            logger.error(f"Concurrent limiter error: {e}")
            return None

    async def release_concurrent_slot(self, organization_id: int, slot_id: str) -> bool:
        """
        Release a concurrent call slot.
        Returns True if slot was released, False otherwise.
        """
        if not slot_id:
            return False

        redis_client = await self._get_redis()
        concurrent_key = f"concurrent_calls:{organization_id}"

        try:
            removed = await redis_client.zrem(concurrent_key, slot_id)
            if removed:
                logger.debug(
                    f"Released concurrent slot {slot_id} for org {organization_id}"
                )
            return bool(removed)
        except Exception as e:
            logger.error(f"Error releasing concurrent slot: {e}")
            return False

    async def get_concurrent_count(self, organization_id: int) -> int:
        """
        Get current number of active concurrent calls for an organization.
        Automatically cleans up stale entries.
        """
        redis_client = await self._get_redis()
        concurrent_key = f"concurrent_calls:{organization_id}"

        try:
            # Clean up stale entries first
            stale_cutoff = time.time() - self.stale_call_timeout
            await redis_client.zremrangebyscore(concurrent_key, 0, stale_cutoff)

            # Get current count
            count = await redis_client.zcard(concurrent_key)
            return count
        except Exception as e:
            logger.error(f"Error getting concurrent count: {e}")
            return 0

    async def store_workflow_slot_mapping(
        self, workflow_run_id: int, organization_id: int, slot_id: str
    ) -> bool:
        """
        Store the mapping between workflow_run_id and its concurrent slot.
        Used for cleanup when calls complete.
        """
        redis_client = await self._get_redis()
        mapping_key = f"workflow_slot_mapping:{workflow_run_id}"

        try:
            # Store as a hash with TTL
            await redis_client.hset(
                mapping_key, mapping={"org_id": organization_id, "slot_id": slot_id}
            )
            # Set expiry to match stale timeout
            await redis_client.expire(mapping_key, self.stale_call_timeout)
            return True
        except Exception as e:
            logger.error(f"Error storing workflow slot mapping: {e}")
            return False

    async def get_workflow_slot_mapping(
        self, workflow_run_id: int
    ) -> Optional[tuple[int, str]]:
        """
        Get the concurrent slot mapping for a workflow run.
        Returns (organization_id, slot_id) tuple or None if not found.
        """
        redis_client = await self._get_redis()
        mapping_key = f"workflow_slot_mapping:{workflow_run_id}"

        try:
            mapping = await redis_client.hgetall(mapping_key)
            if mapping and "org_id" in mapping and "slot_id" in mapping:
                return (int(mapping["org_id"]), mapping["slot_id"])
            return None
        except Exception as e:
            logger.error(f"Error getting workflow slot mapping: {e}")
            return None

    async def delete_workflow_slot_mapping(self, workflow_run_id: int) -> bool:
        """
        Delete the workflow slot mapping after releasing the slot.
        """
        redis_client = await self._get_redis()
        mapping_key = f"workflow_slot_mapping:{workflow_run_id}"

        try:
            deleted = await redis_client.delete(mapping_key)
            return bool(deleted)
        except Exception as e:
            logger.error(f"Error deleting workflow slot mapping: {e}")
            return False

    # ======== FROM NUMBER POOL METHODS ========

    @staticmethod
    def _from_number_pool_key(
        organization_id: int, telephony_configuration_id: int | None
    ) -> str:
        return f"from_number_pool:{organization_id}:{telephony_configuration_id}"

    @staticmethod
    def _from_number_leases_key(
        organization_id: int, telephony_configuration_id: int | None
    ) -> str:
        """Key of the ZSET holding one member per live call on this pool.

        Members are ``"<number>|<lease_id>"`` scored by acquire time, so
        abandoned leases fall out by score. Counting per caller ID is a prefix
        scan of the members, NOT a lex range — scores differ per lease, and
        Redis lex ranges are only meaningful when every score is equal.
        """
        return f"from_number_leases:{organization_id}:{telephony_configuration_id}"

    async def initialize_from_number_pool(
        self,
        organization_id: int,
        from_numbers: list[str],
        telephony_configuration_id: int | None,
    ) -> bool:
        """
        Initialize the from_number pool for an organization + telephony config.
        Uses ZADD NX so it won't overwrite numbers that are already in use.

        Pools are scoped per (organization_id, telephony_configuration_id) so
        that orgs with multiple telephony configurations do not leak caller IDs
        across configs.

        The pool ZSET is a *registry* of usable caller IDs. Live usage lives in
        the companion leases ZSET, so scores here carry no meaning.
        """
        if not from_numbers:
            return False

        redis_client = await self._get_redis()
        key = self._from_number_pool_key(organization_id, telephony_configuration_id)

        try:
            # ZADD NX: only add members that don't already exist (preserves in-use scores)
            members = {number: 0 for number in from_numbers}
            await redis_client.zadd(key, members, nx=True)
            await redis_client.expire(key, 3600)  # 1 hour TTL
            return True
        except Exception as e:
            logger.error(f"Error initializing from_number pool: {e}")
            return False

    async def acquire_from_number(
        self,
        organization_id: int,
        telephony_configuration_id: int | None,
        calls_per_number: int | None = None,
    ) -> Optional["FromNumberLease"]:
        """
        Atomically take a lease on a caller ID from the
        (organization_id, telephony_configuration_id) pool.

        ``calls_per_number`` is how many simultaneous calls one caller ID may
        carry. None (the default) means unlimited — concurrency is then bounded
        only by the org's concurrent-call limit, so a single configured number
        can serve every slot. A positive value caps per-number usage, in which
        case the least-loaded number is chosen so calls spread across DIDs.

        Leases older than ``stale_call_timeout`` are reclaimed first, so a call
        that never released its number can never permanently consume capacity.

        Returns a (number, lease_id) lease, or None when every number is at
        capacity (or the pool is empty).
        """
        redis_client = await self._get_redis()
        pool_key = self._from_number_pool_key(
            organization_id, telephony_configuration_id
        )
        leases_key = self._from_number_leases_key(
            organization_id, telephony_configuration_id
        )
        now = time.time()
        stale_cutoff = now - self.stale_call_timeout
        capacity = calls_per_number if calls_per_number and calls_per_number > 0 else -1
        lease_id = uuid.uuid4().hex
        # Redis seeds Lua's math.random deterministically per call, so rotation
        # has to be driven from outside the script.
        tie_breaker = uuid.uuid4().int % (1 << 31)

        lua_script = """
        local pool_key = KEYS[1]
        local leases_key = KEYS[2]
        local now = tonumber(ARGV[1])
        local stale_cutoff = tonumber(ARGV[2])
        local capacity = tonumber(ARGV[3])
        local lease_id = ARGV[4]
        local tie_breaker = tonumber(ARGV[5])
        local lease_ttl = tonumber(ARGV[6])

        -- Reclaim leases from calls that never released (crash, missed webhook)
        redis.call('ZREMRANGEBYSCORE', leases_key, 0, stale_cutoff)

        local numbers = redis.call('ZRANGE', pool_key, 0, -1)
        if #numbers == 0 then
            return nil
        end

        -- Tally live leases per caller ID. Members are '<number>|<lease_id>'
        -- and scores are acquire timestamps, so this must be a prefix scan --
        -- a lex range would be undefined across differing scores.
        local counts = {}
        if capacity >= 0 then
            local members = redis.call('ZRANGE', leases_key, 0, -1)
            for i = 1, #members do
                local sep = string.find(members[i], '|', 1, true)
                if sep then
                    local num = string.sub(members[i], 1, sep - 1)
                    counts[num] = (counts[num] or 0) + 1
                end
            end
        end

        -- Pick the least-loaded number that is still under capacity
        local best = {}
        local best_count = -1
        for i = 1, #numbers do
            local num = numbers[i]
            local in_use = counts[num] or 0
            if capacity < 0 or in_use < capacity then
                if best_count < 0 or in_use < best_count then
                    best_count = in_use
                    best = {num}
                elseif in_use == best_count then
                    table.insert(best, num)
                end
            end
        end

        if #best == 0 then
            return nil
        end

        local chosen = best[(tie_breaker % #best) + 1]
        redis.call('ZADD', leases_key, now, chosen .. '|' .. lease_id)
        redis.call('EXPIRE', leases_key, lease_ttl)
        -- Clear any in-use score left by the pre-lease pool format
        redis.call('ZADD', pool_key, 'XX', 0, chosen)
        return chosen
        """

        try:
            result = await redis_client.eval(
                lua_script,
                2,
                pool_key,
                leases_key,
                now,
                stale_cutoff,
                capacity,
                lease_id,
                tie_breaker,
                self._lease_ttl,
            )
            if not result:
                return None
            logger.debug(f"Acquired from_number {result} for org {organization_id}")
            return FromNumberLease(number=result, lease_id=lease_id)
        except Exception as e:
            logger.error(f"Error acquiring from_number: {e}")
            return None

    async def release_from_number(
        self,
        organization_id: int,
        from_number: str,
        telephony_configuration_id: int | None,
        lease_id: str | None = None,
    ) -> bool:
        """
        Release one lease on ``from_number`` back to its (org, telephony config)
        pool. Idempotent: releasing an already-released lease returns False.

        ``lease_id`` identifies the exact call's lease. When it is missing — a
        run dispatched before leases existed, or a mapping that lost the field —
        the oldest outstanding lease for that number is dropped instead, so
        capacity is still returned rather than leaked until the stale sweep.
        """
        if not from_number:
            return False

        redis_client = await self._get_redis()
        leases_key = self._from_number_leases_key(
            organization_id, telephony_configuration_id
        )
        pool_key = self._from_number_pool_key(
            organization_id, telephony_configuration_id
        )

        lua_script = """
        local leases_key = KEYS[1]
        local pool_key = KEYS[2]
        local from_number = ARGV[1]
        local lease_id = ARGV[2]

        if lease_id ~= '' then
            -- A known lease id releases exactly its own hold. If it is already
            -- gone this is a duplicate release (webhooks retry): report "not
            -- released" rather than falling through, or a retry would steal a
            -- DIFFERENT live call's lease and over-subscribe the caller ID.
            return redis.call('ZREM', leases_key, from_number .. '|' .. lease_id)
        end

        -- No lease id at all: a mapping written before leases existed. Drop
        -- this number's oldest outstanding lease so capacity is still returned.
        -- ZRANGE is score-ordered (oldest first); prefix-match rather than
        -- lex-range because scores differ per lease.
        local prefix = from_number .. '|'
        local members = redis.call('ZRANGE', leases_key, 0, -1)
        for i = 1, #members do
            if string.sub(members[i], 1, #prefix) == prefix then
                redis.call('ZREM', leases_key, members[i])
                return 1
            end
        end

        -- Legacy pool format: the number itself carried an in-use score
        local score = redis.call('ZSCORE', pool_key, from_number)
        if score and tonumber(score) > 0 then
            redis.call('ZADD', pool_key, 0, from_number)
            return 1
        end
        return 0
        """

        try:
            result = await redis_client.eval(
                lua_script,
                2,
                leases_key,
                pool_key,
                from_number,
                lease_id or "",
            )
            if result:
                logger.debug(
                    f"Released from_number {from_number} for org {organization_id}"
                )
            return bool(result)
        except Exception as e:
            logger.error(f"Error releasing from_number: {e}")
            return False

    async def count_from_number_leases(
        self,
        organization_id: int,
        telephony_configuration_id: int | None,
        from_number: str | None = None,
    ) -> int:
        """Live leases on this pool, or on one caller ID within it.

        Diagnostics and tests only — the dispatcher never needs this.
        """
        redis_client = await self._get_redis()
        leases_key = self._from_number_leases_key(
            organization_id, telephony_configuration_id
        )
        try:
            if from_number is None:
                return int(await redis_client.zcard(leases_key))
            prefix = f"{from_number}|"
            members = await redis_client.zrange(leases_key, 0, -1)
            return sum(1 for member in members if member.startswith(prefix))
        except Exception as e:
            logger.error(f"Error counting from_number leases: {e}")
            return 0

    async def store_workflow_from_number_mapping(
        self,
        workflow_run_id: int,
        organization_id: int,
        from_number: str,
        telephony_configuration_id: int | None,
        lease_id: str | None = None,
    ) -> bool:
        """
        Store the mapping between workflow_run_id and its from_number, plus
        the telephony_configuration_id so cleanup can release back to the
        correct pool and the lease_id identifying this call's specific hold.
        """
        redis_client = await self._get_redis()
        mapping_key = f"workflow_from_number:{workflow_run_id}"

        try:
            # Redis hashes can't store None — use empty string sentinel for legacy
            # campaigns whose telephony_configuration_id has not been backfilled.
            tcid_value = (
                "" if telephony_configuration_id is None else telephony_configuration_id
            )
            await redis_client.hset(
                mapping_key,
                mapping={
                    "org_id": organization_id,
                    "from_number": from_number,
                    "telephony_configuration_id": tcid_value,
                    "lease_id": lease_id or "",
                },
            )
            # Must outlive the longest call, or release finds no mapping and the
            # lease lingers until the stale sweep.
            await redis_client.expire(mapping_key, self._lease_ttl)
            return True
        except Exception as e:
            logger.error(f"Error storing workflow from_number mapping: {e}")
            return False

    async def get_workflow_from_number_mapping(
        self, workflow_run_id: int
    ) -> Optional[tuple[int, str, int | None, str | None]]:
        """
        Get the from_number mapping for a workflow run.
        Returns (organization_id, from_number, telephony_configuration_id,
        lease_id) or None if not found. The last two are None for entries
        written before they existed.
        """
        redis_client = await self._get_redis()
        mapping_key = f"workflow_from_number:{workflow_run_id}"

        try:
            mapping = await redis_client.hgetall(mapping_key)
            if mapping and "org_id" in mapping and "from_number" in mapping:
                raw_tcid = mapping.get("telephony_configuration_id", "")
                tcid = int(raw_tcid) if raw_tcid not in (None, "") else None
                lease_id = mapping.get("lease_id") or None
                return (
                    int(mapping["org_id"]),
                    mapping["from_number"],
                    tcid,
                    lease_id,
                )
            return None
        except Exception as e:
            logger.error(f"Error getting workflow from_number mapping: {e}")
            return None

    async def delete_workflow_from_number_mapping(self, workflow_run_id: int) -> bool:
        """
        Delete the workflow from_number mapping after releasing the number.
        """
        redis_client = await self._get_redis()
        mapping_key = f"workflow_from_number:{workflow_run_id}"

        try:
            deleted = await redis_client.delete(mapping_key)
            return bool(deleted)
        except Exception as e:
            logger.error(f"Error deleting workflow from_number mapping: {e}")
            return False

    async def close(self):
        """Close Redis connection"""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None


# Global rate limiter instance
rate_limiter = RateLimiter()
