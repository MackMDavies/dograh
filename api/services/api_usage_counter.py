"""Per-API-key request metering (Phase 2c).

Hot path: `record_api_request` fire-and-forget INCRs a Redis counter per API key on
every authenticated request — never blocks or raises into the auth flow.

Batched: `flush_api_request_usage` is an hourly ARQ cron that reads+resets each
counter and POSTs the delta to the Sysevo `api-usage-report` edge function, which
applies the plan's free allowance + per-1k rate and accrues to the API billing
account. The edge function is idempotent per (key, period-hour), so a re-report of
the same hour is a no-op.

No-ops silently if no Sysevo edge-function URL is configured.
"""

import asyncio
import os
from datetime import datetime, timezone

import httpx
from loguru import logger

_PREFIX = "apiusage:req:"
_TIMEOUT = 10.0


def _to_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def _incr(api_key_id: int) -> None:
    try:
        from api.tasks.arq import get_arq_redis  # lazy: avoid heavy/circular import on auth path

        redis = await get_arq_redis()
        await redis.incr(f"{_PREFIX}{api_key_id}")
    except Exception as e:  # never let usage counting affect auth
        logger.debug(f"[api-usage] incr failed key={api_key_id}: {e}")


def record_api_request(api_key_id: int) -> None:
    """Fire-and-forget: increment the per-key request counter. Never blocks the caller."""
    try:
        asyncio.create_task(_incr(api_key_id))
    except RuntimeError:
        # No running event loop (shouldn't happen inside a request) — skip silently.
        pass


def _report_url() -> str | None:
    url = os.getenv("SYSEVO_USAGE_REPORT_URL")
    if url:
        return url
    for _env in (
        "SYSEVO_WALLET_DEBIT_URL",
        "SYSEVO_POST_CALL_MEMORY_URL",
        "SYSEVO_PRE_CALL_CHECK_URL",
        "SYSEVO_MEMORY_PRE_CALL_URL",
    ):
        _u = os.getenv(_env)
        if _u:
            return f"{_u.rsplit('/', 1)[0]}/api-usage-report"
    return None


async def flush_api_request_usage(ctx) -> None:
    """ARQ hourly cron: flush per-key request counters to the Sysevo api-usage-report fn.

    Uses GETDEL (atomic read+reset). If the POST then fails the count is dropped rather
    than re-sent — deliberately customer-safe (slight undercount) over risking a
    double-charge. The Supabase side additionally dedupes by (key, period).
    """
    report_url = _report_url()
    if not report_url:
        return

    from api.tasks.arq import get_arq_redis  # lazy import

    redis = await get_arq_redis()
    period = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Sysevo-Secret"] = secret

    # Collect all counter keys via SCAN (non-blocking on the Redis server).
    keys: list = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor, match=f"{_PREFIX}*", count=200)
        keys.extend(batch)
        if cursor == 0:
            break

    if not keys:
        return

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for k in keys:
            key_str = k.decode() if isinstance(k, (bytes, bytearray)) else k
            try:
                count = _to_int(await redis.getdel(key_str))
                if count <= 0:
                    continue
                api_key_id = int(key_str.rsplit(":", 1)[1])
                await client.post(
                    report_url,
                    json={
                        "api_key_id": api_key_id,
                        "kind": "api_request",
                        "count": count,
                        "period": period,
                    },
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[api-usage] flush failed for {key_str}: {e}")
