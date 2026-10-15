from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.admin_telephony import router
from api.services.auth.depends import get_superuser


def test_dialer_protected_numbers_returns_workflow_assigned_twilio_numbers():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_superuser] = lambda: SimpleNamespace(
        id=1, is_superuser=True
    )

    with patch("api.routes.admin_telephony.db_client") as db:
        db.list_active_workflow_assigned_phone_numbers_for_provider = AsyncMock(
            return_value=["+12393231195", "+17867964327"]
        )
        response = TestClient(app).get(
            "/admin/telephony/dialer-protected-numbers"
        )

    assert response.status_code == 200
    assert response.json() == {"numbers": ["+12393231195", "+17867964327"]}
    db.list_active_workflow_assigned_phone_numbers_for_provider.assert_awaited_once_with(
        "twilio"
    )
