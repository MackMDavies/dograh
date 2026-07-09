import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api.services.workflow_active_check import check_workflow_active


@pytest.mark.asyncio
async def test_active_workflow_allowed():
    wf = SimpleNamespace(status="active")
    with patch(
        "api.services.workflow_active_check.db_client.get_workflow_by_id",
        AsyncMock(return_value=wf),
    ):
        ok, reason = await check_workflow_active(1)
    assert ok is True and reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["deactivated", "archived"])
async def test_non_active_blocked(status):
    wf = SimpleNamespace(status=status)
    with patch(
        "api.services.workflow_active_check.db_client.get_workflow_by_id",
        AsyncMock(return_value=wf),
    ):
        ok, reason = await check_workflow_active(1)
    assert ok is False and reason == f"workflow_{status}"


@pytest.mark.asyncio
async def test_missing_workflow_blocked():
    with patch(
        "api.services.workflow_active_check.db_client.get_workflow_by_id",
        AsyncMock(return_value=None),
    ):
        ok, reason = await check_workflow_active(999)
    assert ok is False and reason == "not_found"
