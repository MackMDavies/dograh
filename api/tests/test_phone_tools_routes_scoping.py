"""Organization scoping for the built-in phone-tools settings API."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.organization import router
from api.services.auth.depends import get_user


def _client(user: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: user
    return TestClient(app)


def test_phone_tools_read_uses_authenticated_organizations_policy():
    client = _client(
        SimpleNamespace(id=7, is_superuser=False, selected_organization_id=41)
    )

    with patch(
        "api.services.phone_tools.policy.db_client.get_configuration",
        new_callable=AsyncMock,
    ) as get:
        get.return_value = SimpleNamespace(value={"tools": {"keypad": {"mode": "off"}}})
        response = client.get("/organizations/phone-tools")

    assert response.status_code == 200
    assert response.json()["tools"]["keypad"]["mode"] == "off"
    get.assert_awaited_once_with(41, "PHONE_TOOLS_POLICY")


def test_phone_tools_write_uses_authenticated_organization_and_ignores_body_org_id():
    client = _client(
        SimpleNamespace(id=7, is_superuser=False, selected_organization_id=41)
    )
    body = {
        "organization_id": 92,
        "tools": {"keypad": {"mode": "off", "rule": ""}},
    }

    with patch(
        "api.services.phone_tools.policy.db_client.upsert_configuration",
        new_callable=AsyncMock,
    ) as upsert:
        response = client.put("/organizations/phone-tools", json=body)

    assert response.status_code == 200
    upsert.assert_awaited_once()
    organization_id, key, value = upsert.await_args.args
    assert organization_id == 41
    assert organization_id != body["organization_id"]
    assert key == "PHONE_TOOLS_POLICY"
    assert value["tools"]["keypad"]["mode"] == "off"
