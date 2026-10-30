"""Opt-in real PostgreSQL checks for cross-worker run and media claims.

The model fixture contains only columns used by these methods. It verifies
the actual client methods/transactions, not the application's migration chain.
"""
import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import Boolean, Column, ForeignKey, Integer, JSON, MetaData, String, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import registry, relationship

from api.db import workflow_run_client as module


@pytest.fixture
async def local_db(monkeypatch):
    connection = os.environ.get("DIALER_LOCAL_TEST_DATABASE_URL")
    if not connection:
        pytest.skip("Set DIALER_LOCAL_TEST_DATABASE_URL to the disposable local dialer database")
    url = make_url(connection)
    if not (
        (url.database or "").startswith("dialer_parallel_test")
        and str(url.query.get("host", "")).startswith("/private/tmp/dialer-pg-")
        and str(url.query.get("port", "")) == "55440"
        and url.host is None
    ):
        raise ValueError("This test may only use the isolated local dialer fixture")
    schema = "dialer_agent_orm_" + uuid.uuid4().hex
    base = registry(metadata=MetaData(schema=schema)).generate_base()

    class User(base):
        __tablename__ = "users"
        id = Column(Integer,primary_key=True)

    class Workflow(base):
        __tablename__ = "workflows"
        id = Column(Integer,primary_key=True)
        user_id = Column(Integer,ForeignKey(f"{schema}.users.id"))
        organization_id = Column(Integer)
        user = relationship(User)
        released_definition_id = Column(Integer)
        template_context_variables = Column(JSON)

    class Definition(base):
        __tablename__ = "workflow_definitions"
        id = Column(Integer,primary_key=True)
        workflow_id = Column(Integer)
        status = Column(String)
        is_current = Column(Boolean)
        template_context_variables = Column(JSON)

    class Run(base):
        __tablename__ = "workflow_runs"
        id = Column(Integer,primary_key=True)
        name = Column(String)
        workflow_id = Column(Integer,ForeignKey(f"{schema}.workflows.id"))
        workflow = relationship(Workflow)
        mode = Column(String)
        definition_id = Column(Integer)
        initial_context = Column(JSON)
        gathered_context = Column(JSON)
        logs = Column(JSON)
        campaign_id = Column(Integer)
        queued_run_id = Column(Integer)
        storage_backend = Column(String)
        call_type = Column(String)
        api_key_id = Column(Integer)
        state = Column(String,default="initialized")
        is_completed = Column(Boolean,default=False)

    engine = create_async_engine(connection)
    sessions = async_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.execute(text(f'create schema "{schema}"'))
        await conn.run_sync(base.metadata.create_all)
    async with sessions() as session:
        session.add_all([User(id=99),Definition(id=7,workflow_id=33,status="published",is_current=True)])
        await session.flush()
        session.add(Workflow(id=33,user_id=99,organization_id=11,released_definition_id=7))
        await session.commit()
    monkeypatch.setattr(module,"WorkflowModel",Workflow)
    monkeypatch.setattr(module,"WorkflowRunModel",Run)
    monkeypatch.setattr(module,"WorkflowDefinitionModel",Definition)
    client = module.WorkflowRunClient.__new__(module.WorkflowRunClient)
    client.async_session = sessions
    try:
        yield SimpleNamespace(client=client,sessions=sessions,Run=Run)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'drop schema "{schema}" cascade'))
        await engine.dispose()


def arguments():
    return dict(name="WR-DIALER-duplicate",workflow_id=33,mode="twilio",user_id=99,organization_id=11,
        idempotency_key="one-reserved-line",initial_context={"dialer_original_call_id":"CA-existing","telephony_configuration_id":77},
        gathered_context={"call_id":"CA-existing"})


async def test_parallel_workers_create_one_workflow_run(local_db):
    d = local_db
    a,b = await asyncio.gather(d.client.create_workflow_run(**arguments()),d.client.create_workflow_run(**arguments()))
    assert a.id == b.id
    assert a.initial_context["telephony_configuration_id"] == 77
    async with d.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(d.Run)) == 1


async def test_run_creation_and_media_claim_enforce_org_and_exact_call(local_db):
    d = local_db
    run = await d.client.create_workflow_run(**arguments())
    assert await d.client.claim_dialer_agent_media(run.id,12,"CA-existing") is False
    assert await d.client.claim_dialer_agent_media(run.id,11,"another-call") is False
    results = await asyncio.gather(
        d.client.claim_dialer_agent_media(run.id,11,"CA-existing"),
        d.client.claim_dialer_agent_media(run.id,11,"CA-existing"),
    )
    assert sorted(results) == [False,True]
    async with d.sessions() as session:
        stored = await session.get(d.Run,run.id)
        assert stored.gathered_context["dialer_media_authenticated"] is True
        assert stored.gathered_context["call_id"] == "CA-existing"
    assert await d.client.claim_dialer_agent_media(run.id,11,"CA-existing") is False
    wrong_org = {**arguments(),"organization_id":12,"idempotency_key":"wrong-account"}
    with pytest.raises(ValueError,match="not found"):
        await d.client.create_workflow_run(**wrong_org)


async def test_ended_call_cannot_start_its_prepared_media_later(local_db):
    d = local_db
    run = await d.client.create_workflow_run(**arguments())
    assert await d.client.finish_unstarted_dialer_agent_run(run.id,12,"CA-existing") is False
    assert await d.client.finish_unstarted_dialer_agent_run(run.id,11,"different-call") is False
    assert await d.client.finish_unstarted_dialer_agent_run(run.id,11,"CA-existing") is True
    assert await d.client.finish_unstarted_dialer_agent_run(run.id,11,"CA-existing") is False
    assert await d.client.claim_dialer_agent_media(run.id,11,"CA-existing") is False


@pytest.mark.parametrize("empty_context",["json-null","sql-null","empty-object"])
async def test_media_claim_initializes_empty_or_null_context(local_db,empty_context):
    d = local_db
    run = await d.client.create_workflow_run(**arguments())
    async with d.sessions() as session:
        if empty_context == "sql-null":
            await session.execute(d.Run.__table__.update().where(d.Run.id==run.id).values(gathered_context=text("null")))
        else:
            stored = await session.get(d.Run,run.id)
            stored.gathered_context = None if empty_context=="json-null" else {}
        await session.commit()
    assert await d.client.claim_dialer_agent_media(run.id,11,"CA-existing") is True
    async with d.sessions() as session:
        stored = await session.get(d.Run,run.id)
        assert stored.gathered_context == {"dialer_media_authenticated":True}
