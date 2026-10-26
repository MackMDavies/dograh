"""Execute the production statistics SQL in a rollback-only local PostgreSQL schema."""
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL, ENVIRONMENT
from api.db.api_request_log_client import ApiRequestLogClient


@pytest.fixture
async def stats_client():
    assert ENVIRONMENT == 'test' and urlparse(DATABASE_URL).hostname in {'localhost', '127.0.0.1'}
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        schema = 'voice_stats_proof_' + uuid.uuid4().hex
        await connection.execute(text('CREATE SCHEMA ' + schema))
        await connection.execute(text('SET LOCAL search_path TO ' + schema))
        for sql in [
            'CREATE TABLE api_keys(id integer primary key,organization_id integer,created_at timestamptz)',
            'CREATE TABLE workflow_runs(api_key_id integer,cost_info json,created_at timestamptz)',
            'CREATE TABLE api_request_logs(organization_id integer,api_key_id integer,surface text,status integer,latency_ms integer,created_at timestamptz)',
            "INSERT INTO api_keys VALUES(11,101,'2026-01-01'),(22,202,'2026-01-01')",
            "INSERT INTO workflow_runs VALUES(11,json_build_object('call_duration_seconds',600),'2026-03-01'),(11,json_build_object('call_duration_seconds',120),'2026-02-28'),(22,json_build_object('call_duration_seconds',9000),'2026-03-01')",
            "INSERT INTO api_request_logs VALUES(101,11,'rest',200,100,'2026-03-01'),(101,11,'mcp',500,300,'2026-03-01'),(202,22,'public',200,900,'2026-03-01')",
        ]:
            await connection.execute(text(sql))
        client = object.__new__(ApiRequestLogClient)
        client.async_session = async_sessionmaker(bind=connection, join_transaction_mode='create_savepoint')
        try:
            yield client
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_statistics_use_run_dates_and_only_the_requested_workspace(stats_client, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr('api.db.api_request_log_client.datetime', Clock)
    stats = await stats_client.api_request_stats(101)
    assert stats['minutes'] == {'this_month': 10.0, 'last_month': 2.0}
    assert stats['month'] == {'rest': 1, 'public': 0, 'mcp': 1, 'total': 2, 'errors': 1}
    assert stats['latency_ms'] == {'p50': 200, 'p95': 290}
    assert stats['window'] == {'requests': 2, 'errors': 1, 'top_error_status': 500}
    assert len(stats['days']) == 30
    assert [k['api_key_id'] for k in stats['keys']] == [11]
    empty = await stats_client.api_request_stats(303)
    assert empty['minutes'] == {'this_month': 0.0, 'last_month': 0.0}
    assert empty['window']['requests'] == 0
    assert empty['keys'] == []
