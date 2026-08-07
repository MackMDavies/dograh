"""Tests for check_dial_permitted — the pre-dial suppression/IVR-blocked gate."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.services.dial_permission_check import check_dial_permitted


@pytest.mark.asyncio
async def test_no_op_when_check_url_unset(monkeypatch):
    monkeypatch.delenv("SYSEVO_PRE_CALL_CHECK_URL", raising=False)
    allowed, reason = await check_dial_permitted(200, "+16592127650")
    assert allowed is True
    assert reason == ""


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

        allowed, reason = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
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

        allowed, reason = await check_dial_permitted(200, "+16592127650")

        assert allowed is False
        assert reason == "suppressed"


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

        allowed, reason = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""


@pytest.mark.asyncio
async def test_allows_on_timeout(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("timed out")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        allowed, reason = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""


@pytest.mark.asyncio
async def test_allows_on_unexpected_exception(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.test/check")
    with patch("api.services.dial_permission_check.httpx.AsyncClient") as mock_client_class:
        mock_client_class.side_effect = RuntimeError("boom")

        allowed, reason = await check_dial_permitted(200, "+16592127650")

        assert allowed is True
        assert reason == ""
