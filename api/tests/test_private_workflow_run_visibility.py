from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from api.db.models import WorkflowRunModel
from api.services.workflow.test_sandbox_mode import SANDBOX_ANNOTATION
from api.tests import test_workflow_versioning

org_and_user = test_workflow_versioning.org_and_user
workflow_with_v1 = test_workflow_versioning.workflow_with_v1


@pytest.mark.asyncio
async def test_private_marker_is_committed_at_creation_before_any_provider_or_binding(
    db_session, workflow_with_v1, org_and_user
):
    org, user = org_and_user
    workflow, _ = workflow_with_v1
    private = await db_session.create_workflow_run(
        name="Private voice visibility fixture",
        workflow_id=workflow.id,
        mode="smallwebrtc",
        user_id=user.id,
        organization_id=org.id,
        annotations={SANDBOX_ANNOTATION: {"enabled": True}},
    )
    # No post-creation annotation update is needed to hide the new execution.
    assert await db_session.get_workflow_run(private.id, organization_id=org.id) is None
    assert (await db_session.get_workflow_run_by_id(private.id)).annotations[
        SANDBOX_ANNOTATION
    ]["enabled"] is True
    assert (
        await db_session.get_workflow_run(
            private.id, organization_id=org.id, include_sandbox=True
        )
        is not None
    )
    assert (
        await db_session.get_workflow_run(
            private.id, organization_id=org.id + 10000, include_sandbox=True
        )
        is None
    )


@pytest.mark.asyncio
async def test_customer_history_and_reports_exclude_tests_and_keep_normal_calls(
    db_session, async_session, workflow_with_v1, org_and_user
):
    org, user = org_and_user
    workflow, _ = workflow_with_v1
    runs = []
    for name, annotations in [
        ("ordinary", {}),
        ("disabled", {SANDBOX_ANNOTATION: {"enabled": False}}),
        ("private", {SANDBOX_ANNOTATION: {"enabled": True}}),
    ]:
        run = await db_session.create_workflow_run(
            name=name,
            workflow_id=workflow.id,
            mode="smallwebrtc",
            user_id=user.id,
            organization_id=org.id,
            annotations=annotations,
        )
        await db_session.update_workflow_run(
            run.id,
            is_completed=True,
            cost_info={"call_duration_seconds": 60},
            usage_info={
                "call_duration_seconds": 60,
                "llm": {"FixtureLLM|||fixture-model": {"total_tokens": 10}},
            },
        )
        runs.append(run)
    normal, disabled, private = runs
    history, count = await db_session.get_workflow_runs_by_workflow_id(
        workflow.id, organization_id=org.id
    )
    assert count == 2 and {r.id for r in history} == {normal.id, disabled.id}
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    daily = await db_session.get_workflow_runs_for_daily_report(
        org.id, start, end, workflow.id
    )
    assert {r["id"] for r in daily} == {normal.id, disabled.id}
    csv_rows = await db_session.get_completed_runs_for_report(workflow_id=workflow.id)
    assert {r.id for r in csv_rows} == {normal.id, disabled.id}
    usage = await db_session.get_usage_runs_for_report(org.id)
    assert {r.id for r in usage} == {normal.id, disabled.id}
    usage_history, usage_count, _, duration = await db_session.get_usage_history(org.id)
    assert usage_count == 2 and duration == 120
    assert {r["id"] for r in usage_history} == {normal.id, disabled.id}
    breakdown = await db_session.get_daily_usage_breakdown(org.id, start, end, 0.01)
    assert breakdown["total_minutes"] == 2
    assert sum(day["call_count"] for day in breakdown["breakdown"]) == 2
    model_usage = await db_session.get_usage_by_model(org.id)
    assert model_usage["FixtureLLM|||fixture-model"]["call_count"] == 2
    assert model_usage["FixtureLLM|||fixture-model"]["total_tokens"] == 20
    assert await db_session.ensure_public_access_token(private.id) is None
    normal_token = await db_session.ensure_public_access_token(normal.id)
    assert (
        await db_session.get_workflow_run_by_public_token(normal_token)
    ).id == normal.id
    private_token = str(uuid4())
    await async_session.execute(
        update(WorkflowRunModel)
        .where(WorkflowRunModel.id == private.id)
        .values(public_access_token=private_token)
    )
    await async_session.flush()
    assert await db_session.get_workflow_run_by_public_token(private_token) is None


@pytest.mark.asyncio
async def test_even_superusers_cannot_get_standard_public_storage_links_for_private_tests(
    monkeypatch,
):
    from api.routes import s3_signed_url

    run = SimpleNamespace(annotations={SANDBOX_ANNOTATION: {"enabled": True}})
    monkeypatch.setattr(
        s3_signed_url.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)
    )
    with pytest.raises(HTTPException) as error:
        await s3_signed_url._authorize_and_get_workflow_run(
            7, SimpleNamespace(is_superuser=True)
        )
    assert error.value.status_code == 404
