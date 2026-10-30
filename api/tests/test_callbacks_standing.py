"""Callbacks: every agent gets its queue, a dead callback becomes a task, the agent remembers why."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.callbacks import standing


@pytest.mark.asyncio
async def test_the_queue_is_created_once_per_agent():
    workflow = SimpleNamespace(id=12, name="Front desk", user_id=3, organization_id=4)
    created = SimpleNamespace(id=99)
    running = SimpleNamespace(id=99, is_standing=True, state="running", retry_config={})
    db = SimpleNamespace(
        get_standing_campaign_for_workflow=AsyncMock(return_value=None),
        create_campaign=AsyncMock(return_value=created),
        update_campaign=AsyncMock(return_value=running),
    )
    with patch.object(standing, "db_client", db):
        campaign, was_created = await standing.ensure_standing_campaign(workflow)
    assert was_created and campaign is running
    kwargs = db.create_campaign.await_args.kwargs
    assert kwargs["calling_hours_config"] == {"mode": "inherit"}  # callbacks obey calling hours
    assert kwargs["retry_config"]["retry_delay_seconds"] == 1800
    assert db.update_campaign.await_args.kwargs["is_standing"] is True
    assert db.update_campaign.await_args.kwargs["state"] == "running"

    db.get_standing_campaign_for_workflow = AsyncMock(return_value=running)
    db.create_campaign.reset_mock()
    with patch.object(standing, "db_client", db):
        campaign, was_created = await standing.ensure_standing_campaign(workflow)
    assert not was_created and db.create_campaign.await_count == 0


def test_the_agent_is_told_why_it_is_calling_back():
    text = standing.callback_instruction({
        "origin_workflow_run_id": "88", "reason": "wants a quote for the boiler",
        "callback_raw_phrase": "ring me after three", "callback_summary": "Has a 10-year-old Worcester boiler.",
    })
    assert "callback the person asked for" in text
    assert "boiler" in text and "ring me after three" in text and "Worcester" in text
    assert standing.callback_instruction({"reason": "x"}) is None  # not a callback run
    assert standing.callback_instruction({"origin_workflow_run_id": "1"}) is None  # nothing to say


@pytest.mark.asyncio
async def test_an_unanswered_callback_is_reported_for_a_task(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://ref.supabase.co/functions/v1/dograh-pre-call-check")
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "s")
    monkeypatch.delenv("SYSEVO_CALLBACK_FAILED_URL", raising=False)
    posted = {}

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers, json):
            posted.update(url=url, headers=headers, json=json)
            return SimpleNamespace(status_code=200)

    campaign = SimpleNamespace(id=5, workflow_id=12, is_standing=True)
    run = SimpleNamespace(id=7, retry_count=2, context_variables={"phone_number": "+15550001111", "reason": "quote", "origin_workflow_run_id": "88"})
    with patch.object(standing.httpx, "AsyncClient", Client):
        await standing.report_final_failure(campaign, run, "no_answer")
    assert posted["url"].endswith("/dograh-callback-failed")
    assert posted["headers"] == {"X-Sysevo-Secret": "s"}
    assert posted["json"]["attempts"] == 3 and posted["json"]["reason"] == "quote"

    posted.clear()
    with patch.object(standing.httpx, "AsyncClient", Client):
        await standing.report_final_failure(SimpleNamespace(id=6, workflow_id=1, is_standing=False), run, "no_answer")
    assert posted == {}  # ordinary campaigns are not callbacks


@pytest.mark.asyncio
async def test_the_orchestrator_reports_only_standing_failures():
    from api.services.campaign import campaign_orchestrator as orch_module

    orch = orch_module.CampaignOrchestrator.__new__(orch_module.CampaignOrchestrator)
    campaign = SimpleNamespace(id=5, failed_rows=0, is_standing=True, workflow_id=12)
    with patch.object(orch_module.db_client, "get_campaign_by_id", AsyncMock(return_value=campaign)), \
         patch.object(orch_module.db_client, "update_campaign", AsyncMock()), \
         patch.object(standing, "report_final_failure", AsyncMock()) as report:
        await orch._mark_final_failure(SimpleNamespace(id=7, campaign_id=5), "no_answer")
    report.assert_awaited_once()
