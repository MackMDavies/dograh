from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.workflow import router as workflow_router


def test_test_sandbox_capabilities_requires_internal_secret(monkeypatch):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "expected-internal-secret")
    app = FastAPI()
    app.include_router(workflow_router)

    response = TestClient(app).get("/workflow/test-sandbox/capabilities")

    assert response.status_code == 401
    assert response.json() == {"detail": "Internal sandbox authentication required"}


def test_test_sandbox_capabilities_reports_implemented_protocol(monkeypatch):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "expected-internal-secret")
    app = FastAPI()
    app.include_router(workflow_router)

    response = TestClient(app).get(
        "/workflow/test-sandbox/capabilities",
        headers={"x-sysevo-secret": "expected-internal-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sandbox_mode": True,
        "exact_workflow_version_pinning": True,
        "isolated_action_proxy": True,
        "unsupported_tools_blocked": True,
        "sandbox_protocol_version": 1,
        "supported_test_repair_operations": [
            "append_conciseness_guidance",
            "append_goal_completion_guidance",
            "remove_sandbox_markers",
        ],
    }
