from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.services.telephony.providers.vonage.routes import (
    VonageDialerCallStartRequest,
    VonageDialerCallStatusRequest,
    start_vonage_dialer_call,
    update_vonage_dialer_call_status,
)


_ROUTES = "api.services.telephony.providers.vonage.routes"


@pytest.mark.asyncio
async def test_vonage_call_is_compliance_checked_and_saved_before_origin():
    call_id = uuid4()
    rep_id = str(uuid4())
    user = SimpleNamespace(id=12, provider_id=rep_id)
    with (
        patch(
            f"{_ROUTES}.resolve_assigned_dialer_number",
            new=AsyncMock(return_value={
                "provider": "vonage",
                "direction": "outbound",
                "phone_number": "+14155550100",
            }),
        ),
        patch(f"{_ROUTES}.phone_block_reason", new=AsyncMock(return_value=None)),
        patch(
            f"{_ROUTES}.calling_hours_result",
            new=AsyncMock(return_value=(True, None, "CA")),
        ),
        patch(f"{_ROUTES}.create_dialer_call", new=AsyncMock(return_value=True)) as save_call,
    ):
        response = await start_vonage_dialer_call(
            VonageDialerCallStartRequest(
                call_id=call_id,
                to_number="+14155552671",
                entry_id=uuid4(),
            ),
            user=user,
        )

    assert response["status"] == "initiated"
    assert response["from_number"] == "+14155550100"
    assert save_call.await_args.kwargs["provider"] == "vonage"
    assert save_call.await_args.kwargs["rep_user_id"] == rep_id
    assert save_call.await_args.kwargs["phone_us_state"] == "CA"


@pytest.mark.asyncio
async def test_vonage_call_does_not_start_when_history_cannot_be_saved():
    user = SimpleNamespace(id=12, provider_id=str(uuid4()))
    with (
        patch(
            f"{_ROUTES}.resolve_assigned_dialer_number",
            new=AsyncMock(return_value={
                "provider": "vonage",
                "direction": "both",
                "phone_number": "+14155550100",
            }),
        ),
        patch(f"{_ROUTES}.phone_block_reason", new=AsyncMock(return_value=None)),
        patch(
            f"{_ROUTES}.calling_hours_result",
            new=AsyncMock(return_value=(True, None, "CA")),
        ),
        patch(f"{_ROUTES}.create_dialer_call", new=AsyncMock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc:
            await start_vonage_dialer_call(
                VonageDialerCallStartRequest(call_id=uuid4(), to_number="+14155552671"),
                user=user,
            )

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_vonage_status_uses_rep_scoped_call_log_update():
    call_id = uuid4()
    rep_id = str(uuid4())
    user = SimpleNamespace(provider_id=rep_id)
    with patch(f"{_ROUTES}.update_dialer_call_status", new=AsyncMock()) as update:
        await update_vonage_dialer_call_status(
            VonageDialerCallStatusRequest(
                call_id=call_id,
                status="answered",
                duration_seconds=15,
            ),
            user=user,
        )

    assert update.await_args.kwargs["rep_user_id"] == rep_id
    assert update.await_args.kwargs["provider"] == "vonage"
    assert update.await_args.kwargs["status"] == "in-progress"
