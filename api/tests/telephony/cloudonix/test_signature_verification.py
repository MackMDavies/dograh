"""Regression tests for the Cloudonix webhook signature-verification bypass.

verify_inbound_signature computed the correct x-cx-apikey comparison but
then unconditionally `return True`'d regardless — and neither
handle_cloudonix_status_callback nor handle_cloudonix_cdr called it at all.
Both gaps let anyone forge a call-status webhook (end a call, release
slots, trip the circuit breaker, inject fake cost data) with no auth.
"""

from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from api.services.telephony.providers.cloudonix.provider import CloudonixProvider
from api.services.telephony.providers.cloudonix.routes import (
    handle_cloudonix_cdr,
    handle_cloudonix_status_callback,
)


def _provider() -> CloudonixProvider:
    return CloudonixProvider(
        {
            "bearer_token": "real-cloudonix-key",
            "domain_id": "test1.cloudonix.net",
            "from_numbers": ["+15551230002"],
        }
    )


def _request(*, path: str, json_body: dict, headers: dict[str, str] | None = None) -> Request:
    import json as json_module

    body = json_module.dumps(json_body).encode("utf-8")
    request_headers = [
        (b"content-type", b"application/json"),
        *[
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in (headers or {}).items()
        ],
    ]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("example.test", 443),
            "path": path,
            "query_string": b"",
            "headers": request_headers,
        },
        receive,
    )


class TestVerifyInboundSignature:
    """Direct unit coverage of the provider method itself."""

    @pytest.mark.asyncio
    async def test_matching_api_key_is_valid(self):
        provider = _provider()
        result = await provider.verify_inbound_signature(
            "https://example.test/cloudonix/status-callback/1",
            {},
            {"x-cx-apikey": "real-cloudonix-key"},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_mismatched_api_key_is_invalid(self):
        provider = _provider()
        result = await provider.verify_inbound_signature(
            "https://example.test/cloudonix/status-callback/1",
            {},
            {"x-cx-apikey": "forged-key"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_api_key_is_invalid(self):
        provider = _provider()
        result = await provider.verify_inbound_signature(
            "https://example.test/cloudonix/status-callback/1", {}, {}
        )
        assert result is False


class TestStatusCallbackRoute:
    @pytest.mark.asyncio
    async def test_forged_status_callback_is_rejected(self):
        provider = _provider()
        workflow_run = AsyncMock(workflow_id=42)
        workflow = AsyncMock(organization_id=7)

        with (
            patch("api.services.telephony.providers.cloudonix.routes.db_client") as mock_db,
            patch(
                "api.services.telephony.providers.cloudonix.routes.get_telephony_provider_for_run",
                new=AsyncMock(return_value=provider),
            ),
            patch(
                "api.services.telephony.providers.cloudonix.routes._process_status_update",
                new=AsyncMock(),
            ) as mock_process,
        ):
            mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
            mock_db.get_workflow_by_id = AsyncMock(return_value=workflow)

            request = _request(
                path="/cloudonix/status-callback/1",
                json_body={"status": "completed"},
                headers={"x-cx-apikey": "forged-key"},
            )
            result = await handle_cloudonix_status_callback(1, request)

            assert result == {"status": "error", "reason": "invalid_signature"}
            mock_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_genuine_status_callback_is_processed(self):
        provider = _provider()
        workflow_run = AsyncMock(workflow_id=42)
        workflow = AsyncMock(organization_id=7)

        with (
            patch("api.services.telephony.providers.cloudonix.routes.db_client") as mock_db,
            patch(
                "api.services.telephony.providers.cloudonix.routes.get_telephony_provider_for_run",
                new=AsyncMock(return_value=provider),
            ),
            patch(
                "api.services.telephony.providers.cloudonix.routes._process_status_update",
                new=AsyncMock(),
            ) as mock_process,
        ):
            mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
            mock_db.get_workflow_by_id = AsyncMock(return_value=workflow)

            request = _request(
                path="/cloudonix/status-callback/1",
                json_body={
                    "call_id": "abc",
                    "status": "completed",
                    "from": "+1",
                    "to": "+2",
                },
                headers={"x-cx-apikey": "real-cloudonix-key"},
            )
            result = await handle_cloudonix_status_callback(1, request)

            assert result == {"status": "success"}
            mock_process.assert_awaited_once()


class TestCdrRoute:
    @pytest.mark.asyncio
    async def test_forged_cdr_is_rejected(self):
        provider = _provider()
        workflow_run = AsyncMock(id=1, workflow_id=42)
        workflow = AsyncMock(organization_id=7)

        with (
            patch("api.services.telephony.providers.cloudonix.routes.db_client") as mock_db,
            patch(
                "api.services.telephony.providers.cloudonix.routes.get_telephony_provider_for_run",
                new=AsyncMock(return_value=provider),
            ),
            patch(
                "api.services.telephony.providers.cloudonix.routes._process_status_update",
                new=AsyncMock(),
            ) as mock_process,
        ):
            mock_db.get_workflow_run_by_call_id = AsyncMock(return_value=workflow_run)
            mock_db.get_workflow_by_id = AsyncMock(return_value=workflow)

            request = _request(
                path="/cloudonix/cdr",
                json_body={
                    "domain": "test1.cloudonix.net",
                    "session": {"token": "call-123"},
                    "disposition": "ANSWER",
                },
                headers={"x-cx-apikey": "forged-key"},
            )
            result = await handle_cloudonix_cdr(request)

            assert result == {"status": "error", "reason": "invalid_signature"}
            mock_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_genuine_cdr_is_processed(self):
        provider = _provider()
        workflow_run = AsyncMock(id=1, workflow_id=42)
        workflow = AsyncMock(organization_id=7)

        with (
            patch("api.services.telephony.providers.cloudonix.routes.db_client") as mock_db,
            patch(
                "api.services.telephony.providers.cloudonix.routes.get_telephony_provider_for_run",
                new=AsyncMock(return_value=provider),
            ),
            patch(
                "api.services.telephony.providers.cloudonix.routes._process_status_update",
                new=AsyncMock(),
            ) as mock_process,
        ):
            mock_db.get_workflow_run_by_call_id = AsyncMock(return_value=workflow_run)
            mock_db.get_workflow_by_id = AsyncMock(return_value=workflow)

            request = _request(
                path="/cloudonix/cdr",
                json_body={
                    "domain": "test1.cloudonix.net",
                    "session": {"token": "call-123"},
                    "disposition": "ANSWER",
                },
                headers={"x-cx-apikey": "real-cloudonix-key"},
            )
            result = await handle_cloudonix_cdr(request)

            assert result == {"status": "success"}
            mock_process.assert_awaited_once()
