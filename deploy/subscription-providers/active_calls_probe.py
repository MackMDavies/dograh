"""Read-only deployment guard. Run inside the CURRENT API container."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import redis.asyncio as redis
from sqlalchemy import text
from api.db import db_client


async def main():
    async with db_client.async_session() as session:
        result = await session.execute(text("SELECT count(*) FROM workflow_runs WHERE lower(mode) <> 'textchat' AND is_completed IS NOT TRUE AND (state = 'running' OR (state = 'initialized' AND created_at > now() - interval '20 minutes'))"))
        workflows = result.scalar_one()
    client = redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
    try:
        slots = 0
        async for key in client.scan_iter(match='concurrent_calls:*'):
            slots += await client.zcount(key, time.time() - 1200, '+inf')
        # ARQ retains completed cron locks for uniqueness, but removes their
        # job payload in the same finish transaction. Only live payloads count.
        jobs = 0
        async for key in client.scan_iter(match='arq:in-progress:*'):
            if await client.exists('arq:job:' + key.removeprefix('arq:in-progress:')):
                jobs += 1
    finally:
        await client.aclose()
    url = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1/'
    token = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    headers = {'Authorization': 'Bearer ' + token, 'apikey': token, 'Prefer': 'count=exact'}
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    counts = {}
    async with httpx.AsyncClient(timeout=10) as http:
        for table, timestamp, terminal in (('dialer_calls', 'started_at', 'completed,failed,busy,no-answer,canceled,cancelled'), ('inbound_calls', 'created_at', 'missed,voicemail,failed')):
            response = await http.head(url + table, headers=headers, params={'select': 'id', 'ended_at': 'is.null', timestamp: 'gte.' + since, 'status': 'not.in.(' + terminal + ')'})
            response.raise_for_status()
            total = response.headers.get('content-range', '').split('/')[-1]
            if not total.isdigit():
                raise RuntimeError('Activity count was not confirmed')
            counts[table] = int(total)
    tcp = 0
    udp = 0
    for name in ('tcp', 'tcp6'):
        for line in Path('/proc/net/' + name).read_text().splitlines()[1:]:
            columns = line.split()
            if int(columns[1].rsplit(':', 1)[1], 16) in (8000, 8001) and columns[3] == '01':
                tcp += 1
    for name in ('udp', 'udp6'):
        for line in Path('/proc/net/' + name).read_text().splitlines()[1:]:
            address = line.split()[1].rsplit(':', 1)[0]
            if address not in ('0B00007F', '0000000000000000FFFF00000B00007F'):
                udp += 1
    result = {'active_voice_workflows': workflows, 'concurrency_slots': slots, 'worker_jobs_in_progress': jobs, 'api_connections': tcp, 'media_udp_sockets': udp, **counts}
    result['safe_to_recreate'] = all(value == 0 for value in result.values())
    print(json.dumps(result))
    if not result['safe_to_recreate']:
        raise SystemExit(2)


try:
    asyncio.run(main())
except Exception:
    print(json.dumps({'activity_probe': 'failed', 'safe_to_recreate': False}))
    raise SystemExit(1) from None
