"""Tests for check_dial_permitted — the pre-dial suppression/IVR-blocked gate."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.services.dial_permission_check import check_dial_permitted


@pytest.mark.asyncio
async def test_no_op_when_check_url_unset(monkeypatch):
    monkeypatch.delenv("SYSEVO_PRE_CALL_CHECK_URL", raising=False)
    allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")
    assert allowed is True
    assert reason == ""
    assert retry_at is None


@pytest.mark.asyncio
async def test_allows_when_response_says_not_blocked(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "call_inbound": {"dynamic_variables": {"dial_blocked": "false", "dial_block_reason": ""}}
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
        assert retry_at is None
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["call_inbound"]["agent_id"] == 200
        assert call_kwargs["json"]["call_inbound"]["to_number"] == "+16592127650"


@pytest.mark.asyncio
async def test_blocks_when_response_says_suppressed(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "call_inbound": {"dynamic_variables": {"dial_blocked": "true", "dial_block_reason": "suppressed"}}
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is False
        assert reason == "suppressed"
        assert retry_at is None


@pytest.mark.asyncio
async def test_allows_on_non_success_http_status(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = False
        mock_response.status_code = 500
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
        assert retry_at is None


@pytest.mark.asyncio
async def test_allows_on_timeout(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("timed out")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
        assert retry_at is None


@pytest.mark.asyncio
async def test_allows_on_unexpected_exception(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client_class.side_effect = RuntimeError("boom")

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
        assert retry_at is None


@pytest.mark.asyncio
async def test_passes_campaign_calling_hours_through_when_provided(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "call_inbound": {"dynamic_variables": {"dial_blocked": "false", "dial_block_reason": ""}}
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await check_dial_permitted(
            200, "+16592127650", campaign_calling_hours={"mode": "custom", "start": "09:00", "end": "18:00"}
        )

        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["call_inbound"]["calling_hours"] == {
            "mode": "custom",
            "start": "09:00",
            "end": "18:00",
        }


@pytest.mark.asyncio
async def test_omits_calling_hours_key_when_not_provided(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "call_inbound": {"dynamic_variables": {"dial_blocked": "false", "dial_block_reason": ""}}
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await check_dial_permitted(200, "+16592127650")

        call_kwargs = mock_client.post.call_args.kwargs
        assert "calling_hours" not in call_kwargs["json"]["call_inbound"]


@pytest.mark.asyncio
async def test_surfaces_retry_at_when_blocked_for_calling_hours(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "call_inbound": {
                "dynamic_variables": {
                    "dial_blocked": "true",
                    "dial_block_reason": "outside_calling_hours",
                    "retry_at": "2026-08-09T13:00:00.000Z",
                }
            }
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is False
        assert reason == "outside_calling_hours"
        assert retry_at == "2026-08-09T13:00:00.000Z"


@pytest.mark.asyncio
async def test_retry_at_is_none_when_not_blocked(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "call_inbound": {"dynamic_variables": {"dial_blocked": "false", "dial_block_reason": ""}}
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason, retry_at = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
        assert retry_at is None
