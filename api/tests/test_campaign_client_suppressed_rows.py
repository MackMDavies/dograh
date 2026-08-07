"""increment_campaign_suppressed_rows must issue the same atomic
UPDATE ... SET suppressed_rows = suppressed_rows + 1 shape as the existing
increment_campaign_failed_rows — verified by inspecting the executed SQL
text, since a real Postgres session isn't available in this environment."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.db.campaign_client import CampaignClient


@pytest.fixture
def client():
    with patch("api.db.campaign_client.BaseDBClient.__init__", return_value=None):
        c = CampaignClient()
        return c


@pytest.mark.asyncio
async def test_increments_suppressed_rows(client):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    client.async_session = MagicMock(return_value=mock_session)

    await client.increment_campaign_suppressed_rows(campaign_id=42)

    mock_session.execute.assert_awaited_once()
    executed_sql = str(mock_session.execute.await_args.args[0])
    assert "suppressed_rows = suppressed_rows + 1" in executed_sql
    mock_session.commit.assert_awaited_once()
