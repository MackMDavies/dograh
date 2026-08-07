"""sync_dial_suppression must:
 - no-op silently when the integration isn't configured (mirrors
   reconcile_wallet_debits' _SYSEVO_ENV_VARS guard exactly)
 - group the fetched rows by workflow_id and rebuild each Redis set via
   build-in-a-scratch-key-then-RENAME, never leaving the live key transiently
   empty mid-rebuild
 - never raise out of the task on an HTTP failure — logged and skipped,
   picked up again next cycle
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from api.tasks import dial_suppression_sync  # noqa: E402


@pytest.mark.asyncio
async def test_noops_when_not_configured(monkeypatch):
    monkeypatch.delenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", raising=False)
    mock_redis = AsyncMock()
    with patch.object(dial_suppression_sync, "_get_redis", AsyncMock(return_value=mock_redis)):
        await dial_suppression_sync.sync_dial_suppression(None)
    mock_redis.sadd.assert_not_called()


@pytest.mark.asyncio
async def test_rebuilds_one_set_per_workflow_via_scratch_key_rename(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", "https://example.test/sync")
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "shh")

    response = MagicMock()
    response.status_code = 200
    response.is_success = True
    response.json.return_value = {
        "suppressions": [
            {"dograh_workflow_id": 101, "phone_key": "15095551234"},
            {"dograh_workflow_id": 101, "phone_key": "15095555678"},
            {"dograh_workflow_id": 202, "phone_key": "15095559999"},
        ]
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    mock_redis = AsyncMock()
    mock_redis.sadd = AsyncMock()
    mock_redis.rename = AsyncMock()
    mock_redis.delete = AsyncMock()

    with patch.object(dial_suppression_sync, "_get_redis", AsyncMock(return_value=mock_redis)), \
         patch("api.tasks.dial_suppression_sync.httpx.AsyncClient", return_value=mock_client):
        await dial_suppression_sync.sync_dial_suppression(None)

    sadd_calls = {c.args[0]: c.args[1:] for c in mock_redis.sadd.await_args_list}
    assert sadd_calls["suppress:101:building"] == ("15095551234", "15095555678")
    assert sadd_calls["suppress:202:building"] == ("15095559999",)
    renamed_to = {c.args[1] for c in mock_redis.rename.await_args_list}
    assert renamed_to == {"suppress:101", "suppress:202"}


@pytest.mark.asyncio
async def test_logs_and_returns_on_fetch_failure_without_raising(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIAL_SUPPRESSION_LIST_URL", "https://example.test/sync")
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "shh")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=TimeoutError("unreachable"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_redis = AsyncMock()

    with patch.object(dial_suppression_sync, "_get_redis", AsyncMock(return_value=mock_redis)), \
         patch("api.tasks.dial_suppression_sync.httpx.AsyncClient", return_value=mock_client):
        await dial_suppression_sync.sync_dial_suppression(None)  # must not raise

    mock_redis.sadd.assert_not_called()
