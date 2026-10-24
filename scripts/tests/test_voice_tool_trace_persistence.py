"""Real ORM/JSONB proof on an explicitly identified disposable local cluster.

No DATABASE_URL is used for connections. Each test creates and drops only its
own database after checking the server's data directory against an explicit pin.
"""

import asyncio
import getpass
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.workflow_run_client import WorkflowRunClient

SESSION = "11111111-1111-4111-8111-111111111111"
REPORT = {"version": 1, "sealed_result": "fixture"}
TRACE = {"version": 1, "trace": {"calls": []}}


@pytest_asyncio.fixture
async def isolated_trace_database():
    socket = os.environ.get("PRIVATE_VOICE_TRACE_TEST_SOCKET")
    port = os.environ.get("PRIVATE_VOICE_TRACE_TEST_PORT")
    pinned = os.environ.get("PRIVATE_VOICE_TRACE_TEST_DATA_DIR")
    if not all([socket, port, pinned]):
        pytest.skip("Explicit disposable private-voice trace PostgreSQL settings are required")
    assert Path(socket).resolve().is_relative_to(Path("/private/tmp"))
    assert Path(pinned).resolve().is_relative_to(Path("/private/tmp"))
    assert port.isdigit() and 1024 <= int(port) <= 65535
    admin = await asyncpg.connect(host=socket, port=int(port), user=getpass.getuser(), database="postgres")
    name = "voice_trace_fixture_" + uuid4().hex
    engine = None
    created = False
    try:
        assert Path(await admin.fetchval("show data_directory")).resolve() == Path(pinned).resolve()
        await admin.execute(f'create database "{name}"')
        created = True
        engine = create_async_engine(URL.create("postgresql+asyncpg", username=getpass.getuser(),
                                               host=socket, port=int(port), database=name))
        async with engine.begin() as connection:
            await connection.execute(text("create table workflows(id bigint primary key, organization_id bigint not null)"))
            await connection.execute(text("create table workflow_runs(id bigint primary key, workflow_id bigint not null, annotations jsonb not null)"))
            await connection.execute(text("insert into workflows values(185,42),(186,43)"))
            marker = {"unrelated": {"keep": True}, "sysevo_test_sandbox": {
                "enabled": True, "voice_session_id": SESSION, "organization_id": 42,
            }}
            await connection.execute(text("insert into workflow_runs values(123,185,cast(:annotations as jsonb))"), {"annotations": json.dumps(marker)})
        client = SimpleNamespace(async_session=async_sessionmaker(engine, expire_on_commit=False))

        async def save(result=REPORT, trace=TRACE, **scope):
            return await WorkflowRunClient.save_agent_test_voice_result(
                client, scope.get("run_id",123), scope.get("workflow_id",185), scope.get("organization_id",42),
                scope.get("session_id",SESSION), result, tool_trace=trace,
            )

        async def read():
            async with engine.connect() as connection:
                return (await connection.execute(text("select annotations from workflow_runs where id=123"))).scalar_one()

        yield save, read
    finally:
        if engine is not None: await engine.dispose()
        if created: await admin.execute(f'drop database "{name}"')
        await admin.close()


@pytest.mark.asyncio
async def test_result_and_trace_are_atomic_immutable_and_idempotent(isolated_trace_database):
    save, read = isolated_trace_database
    assert await save() is True
    assert await save() is True
    expected = await read()
    assert expected["unrelated"] == {"keep": True}
    assert expected["agent_test_voice_result"] == REPORT
    assert expected["agent_test_voice_tool_trace"] == TRACE
    assert await save(result={"changed": True}) is False
    assert await save(trace={"changed": True}) is False
    assert await read() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [{"run_id":124},{"workflow_id":186},{"organization_id":43},{"session_id":"wrong"}])
async def test_a_mismatched_run_workflow_account_or_session_cannot_write(scope, isolated_trace_database):
    save, read = isolated_trace_database
    before = await read()
    assert await save(**scope) is False
    assert await read() == before


@pytest.mark.asyncio
async def test_competing_trace_writers_cannot_replace_the_winner(isolated_trace_database):
    save, read = isolated_trace_database
    traces = [{"trace": "one"}, {"trace": "two"}]
    outcomes = await asyncio.gather(*(save(trace=trace) for trace in traces))
    assert sorted(outcomes) == [False, True]
    winner = traces[outcomes.index(True)]
    assert (await read())["agent_test_voice_tool_trace"] == winner


@pytest.mark.asyncio
async def test_older_result_writer_preserves_a_new_trace(isolated_trace_database):
    save, read = isolated_trace_database
    assert await save(trace=None) is True
    assert "agent_test_voice_tool_trace" not in await read()
    assert await save() is True
    expected = await read()
    assert await save(trace=None) is True
    assert await read() == expected
