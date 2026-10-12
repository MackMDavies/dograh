from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.workflow import router
from api.services.auth.depends import get_user


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1,
        provider_id="provider-1",
        selected_organization_id=11,
    )
    return app


def test_workflow_capabilities_report_deactivated_creation_support():
    app = _make_test_app()
    response = TestClient(app).get("/workflow/capabilities")

    assert response.status_code == 200
    assert response.json() == {"supports_deactivated_creation": True}


def test_create_workflow_rejects_invalid_trigger_path_before_db_write():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.workflow.db_client") as mock_db:
        response = client.post(
            "/workflow/create/definition",
            json={
                "name": "Support Agent",
                "workflow_definition": {
                    "nodes": [
                        {
                            "id": "trigger-1",
                            "type": "trigger",
                            "data": {"trigger_path": "support/west"},
                        }
                    ],
                    "edges": [],
                },
            },
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["is_valid"] is False
    assert detail["errors"][0]["field"] == "data.trigger_path"
    assert "single URL path segment" in detail["errors"][0]["message"]
    assert mock_db.mock_calls == []


def test_create_workflow_can_request_deactivated_candidate():
    app = _make_test_app()
    client = TestClient(app)
    workflow = SimpleNamespace(
        id=42,
        name="Agent Testing Candidate",
        status="deactivated",
        created_at=datetime.now(timezone.utc),
        current_definition_id=81,
        template_context_variables={},
        call_disposition_codes={},
        workflow_configurations={},
    )
    with patch("api.routes.workflow.db_client") as mock_db, patch(
        "api.routes.workflow.capture_event"
    ):
        mock_db.create_workflow = AsyncMock(return_value=workflow)
        response = client.post(
            "/workflow/create/definition",
            json={
                "name": "Agent Testing Candidate",
                "workflow_definition": {"nodes": [], "edges": []},
                "status": "deactivated",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "deactivated"
    mock_db.create_workflow.assert_awaited_once_with(
        "Agent Testing Candidate",
        {"nodes": [], "edges": []},
        1,
        11,
        status="deactivated",
    )


def test_create_workflow_rejects_unknown_initial_status():
    app = _make_test_app()
    client = TestClient(app)
    with patch("api.routes.workflow.db_client") as mock_db:
        response = client.post(
            "/workflow/create/definition",
            json={
                "name": "Agent Testing Candidate",
                "workflow_definition": {"nodes": [], "edges": []},
                "status": "active_and_live",
            },
        )

    assert response.status_code == 422
    assert mock_db.mock_calls == []
