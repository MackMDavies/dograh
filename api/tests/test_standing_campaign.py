"""A standing campaign must never auto-complete.

_has_pending_work() counts scheduled runs due NOW. A callback booked for 2pm is
not due at 10am, so a standing campaign holding only future work looks finished
and would be marked completed — after which _check_stale_campaigns (which only
polls `running` campaigns) never looks at it again and the callback never fires.
"""
import pytest

from api.services.campaign.campaign_orchestrator import CampaignOrchestrator


class _Campaign:
    def __init__(self, is_standing):
        self.id = 999
        self.state = "running"
        self.is_standing = is_standing
        self.total_rows = 1
        self.processed_rows = 0
        self.failed_rows = 0


@pytest.mark.asyncio
async def test_standing_campaign_is_not_completed(monkeypatch):
    orch = CampaignOrchestrator.__new__(CampaignOrchestrator)
    completed = []

    async def fake_update(**kwargs):
        completed.append(kwargs)

    monkeypatch.setattr(
        "api.services.campaign.campaign_orchestrator.db_client.update_campaign",
        fake_update,
    )

    await orch._complete_campaign(_Campaign(is_standing=True))
    assert completed == [], "a standing campaign must never be marked completed"


@pytest.mark.asyncio
async def test_ordinary_campaign_still_completes(monkeypatch):
    """The guard must not stop normal campaigns finishing."""
    orch = CampaignOrchestrator.__new__(CampaignOrchestrator)
    calls = []

    async def fake_has_pending(_cid):
        return False

    async def fake_get(_cid):
        return _Campaign(is_standing=False)

    async def fake_update(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(orch, "_has_pending_work", fake_has_pending)
    monkeypatch.setattr(
        "api.services.campaign.campaign_orchestrator.db_client.get_campaign_by_id",
        fake_get,
    )
    monkeypatch.setattr(
        "api.services.campaign.campaign_orchestrator.db_client.update_campaign",
        fake_update,
    )

    await orch._complete_campaign(_Campaign(is_standing=False))
    assert any(c.get("state") in ("completed", "failed") for c in calls)
