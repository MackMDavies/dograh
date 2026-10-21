"""Calling-hours policy (2026-10-21): two-step warning, confirmed override, batches skip."""

from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.dialer import compliance

pytestmark = pytest.mark.asyncio


async def test_batch_dials_only_leads_the_server_confirms():
    async def block(phone):
        return "do_not_call" if phone == "+15550000003" else None

    async def hours(phone, entry):
        return {
            "+15550000002": (False, "outside_calling_hours"),
            "+15550000004": (False, "timezone_unavailable"),
        }.get(phone, (True, None))

    with patch.object(compliance, "phone_block_reason", side_effect=block), \
         patch.object(compliance, "calling_hours_allowed", side_effect=hours):
        keep, skipped = await compliance.callable_batch_entries([
            ("a", "+15550000001"), ("b", "+15550000002"), ("c", "+15550000003"),
            ("d", "+15550000004"), ("e", "+15550000005"),
        ])
    assert keep == ["a", "e"]
    assert skipped == {"b": "outside_calling_hours", "c": "do_not_call", "d": "timezone_unavailable"}


def test_batch_snaps_to_supported_line_counts():
    assert [compliance.snap_parallel_lines(n) for n in range(0, 11)] == [0, 0, 2, 3, 3, 5, 5, 5, 5, 5, 10]


def test_confirmed_override_covers_hours_reasons_never_do_not_call():
    for reason in ("outside_calling_hours", "public_holiday", "timezone_unavailable", "compliance_unavailable"):
        assert compliance.rep_overrides_hours(reason, "1")
        assert not compliance.rep_overrides_hours(reason, "")
    assert not compliance.rep_overrides_hours("do_not_call", "1")


async def test_number_the_lead_row_cannot_vouch_for_is_judged_by_the_number():
    calls = []

    async def raw(phone, entry):
        calls.append(entry)
        if entry:
            return {"allowed": False, "reason": "lead_phone_mismatch"}
        return {"allowed": True, "reason": None, "timezone": "America/New_York"}

    with patch.object(compliance, "_calling_hours_raw", side_effect=raw):
        result = await compliance.calling_hours_detail("+15550000001", "entry-1")
    assert result["allowed"] is True and calls == ["entry-1", None]


async def test_unreachable_hours_service_is_retried_once():
    results = iter([{"allowed": False, "reason": "compliance_unavailable"}, {"allowed": True, "reason": None}])
    with patch.object(compliance, "_calling_hours_raw", new=AsyncMock(side_effect=lambda *_: next(results))), \
         patch.object(compliance.asyncio, "sleep", new=AsyncMock()):
        result = await compliance.calling_hours_detail("+15550000001")
    assert result["allowed"] is True


def _hours_app():
    from types import SimpleNamespace

    from fastapi import FastAPI

    from api.services.auth.sysevo_roles import require_sales_dialer_role
    from api.services.telephony.dialer.handling_routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(provider_id="rep-1")
    return app


def test_browser_asks_the_same_hours_check_the_webhooks_use():
    from fastapi.testclient import TestClient

    detail = {"allowed": False, "reason": "outside_calling_hours", "timezone": "America/Chicago",
              "us_state": "TX", "retry_at": "2026-10-21T13:00:00Z"}
    with patch.object(compliance, "calling_hours_detail", new=AsyncMock(return_value=detail)) as check:
        response = TestClient(_hours_app()).post(
            "/dialer/calling-hours",
            json={"phone": "+1 (555) 000-0001", "entry_id": "11111111-1111-4111-8111-111111111111"},
        )
    assert response.status_code == 200 and response.json() == detail
    check.assert_awaited_once_with("+15550000001", "11111111-1111-4111-8111-111111111111")


def test_hours_check_rejects_an_undialable_number():
    from fastapi.testclient import TestClient

    response = TestClient(_hours_app()).post("/dialer/calling-hours", json={"phone": "hello"})
    assert response.status_code == 400
