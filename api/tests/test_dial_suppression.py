"""is_number_suppressed must:
 - short-circuit to False when the integration isn't configured (OSS deployments
   with no SYSEVO_DIAL_SUPPRESSION_LIST_URL set pay nothing and are never blocked)
 - trust a definitive Redis answer, whichever way it goes
 - fall back to the Supabase check endpoint when Redis itself errors
 - default to True (skip the dial) only when BOTH Redis and the Supabase
   fallback fail — never guess a number is safe to dial during a double outage
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from api.services.campaign import dial_suppression  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    dial_suppression._redis_client = None
    yield
    dial_suppression._redis_client = None


def _mock_httpx_response(json_body, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.json.return_value = json_body
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_returns_false_immediately_when_integration_not_configured(monkeypatch):
    monkeypatch.delenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", raising=False)
    mock_redis = AsyncMock()
    with patch.object(dial_suppression, "_get_redis", AsyncMock(return_value=mock_redis)):
        result = await dial_suppression.is_number_suppressed(101, "+15095551234")
    assert result is False
    mock_redis.sismember.assert_not_called()


@pytest.mark.asyncio
async def test_trusts_redis_true(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", "https://example.test/sync")
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=1)
    with patch.object(dial_suppression, "_get_redis", AsyncMock(return_value=mock_redis)):
        result = await dial_suppression.is_number_suppressed(101, "+15095551234")
    assert result is True


@pytest.mark.asyncio
async def test_trusts_redis_false(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", "https://example.test/sync")
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=0)
    with patch.object(dial_suppression, "_get_redis", AsyncMock(return_value=mock_redis)):
        result = await dial_suppression.is_number_suppressed(101, "+15095551234")
    assert result is False


@pytest.mark.asyncio
async def test_falls_back_to_supabase_check_when_redis_errors(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", "https://example.test/sync")
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "shh")
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(side_effect=ConnectionError("redis down"))
    mock_client = _mock_httpx_response({"suppressed": True})
    with patch.object(dial_suppression, "_get_redis", AsyncMock(return_value=mock_redis)), \
         patch("api.services.campaign.dial_suppression.httpx.AsyncClient", return_value=mock_client):
        result = await dial_suppression.is_number_suppressed(101, "+15095551234")
    assert result is True
    mock_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_defaults_to_suppressed_when_both_redis_and_supabase_fail(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", "https://example.test/sync")
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "shh")
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(side_effect=ConnectionError("redis down"))
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=TimeoutError("supabase unreachable"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch.object(dial_suppression, "_get_redis", AsyncMock(return_value=mock_redis)), \
         patch("api.services.campaign.dial_suppression.httpx.AsyncClient", return_value=mock_client):
        result = await dial_suppression.is_number_suppressed(101, "+15095551234")
    assert result is True
