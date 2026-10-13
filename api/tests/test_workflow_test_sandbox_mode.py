from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.routes.workflow_text_chat import router as text_chat_router
from api.services.auth.depends import get_user
from api.services.workflow.test_sandbox_mode import (
    build_run_annotations,
    is_sandbox_test_run,
    sandbox_post_call_policy,
    sandbox_secret_matches,
)


def test_text_chat_sandbox_mode_rejects_requests_without_internal_secret():
    app = FastAPI()
    app.include_router(text_chat_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1,
        provider_id="test-user",
        selected_organization_id=11,
    )

    response = TestClient(app).post(
        "/workflow/10/text-chat/sessions",
        json={"sandbox_mode": True},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Sandbox test authorization required"


def test_authorized_sandbox_session_requires_run_and_scenario_binding(monkeypatch):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "internal-test-secret")
    app = FastAPI()
    app.include_router(text_chat_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1,
        provider_id="test-user",
        selected_organization_id=11,
    )
    response = TestClient(app).post(
        "/workflow/10/text-chat/sessions",
        json={"sandbox_mode": True},
        headers={"x-sysevo-secret": "internal-test-secret"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Sandbox test run and scenario identifiers are required"


def test_authorized_sandbox_session_requires_exact_version_pin(monkeypatch):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "internal-test-secret")
    app = FastAPI()
    app.include_router(text_chat_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1,
        provider_id="test-user",
        selected_organization_id=11,
    )
    response = TestClient(app).post(
        "/workflow/10/text-chat/sessions",
        json={
            "sandbox_mode": True,
            "annotations": {
                "agent_test_run_id": "12345678-1234-4234-8234-123456789abc",
                "scenario_id": "qa-published-version",
            },
        },
        headers={"x-sysevo-secret": "internal-test-secret"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Sandbox test sessions must bind to exactly one published or candidate draft version"


def test_published_version_pinning_is_not_available_to_regular_text_sessions():
    app = FastAPI()
    app.include_router(text_chat_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1,
        provider_id="test-user",
        selected_organization_id=11,
    )
    response = TestClient(app).post(
        "/workflow/10/text-chat/sessions",
        json={"published_version_number": 4},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Published version pinning is reserved for authenticated sandbox tests"


def test_sandbox_secret_requires_both_matching_values():
    assert sandbox_secret_matches("shared", "shared")
    assert not sandbox_secret_matches("wrong", "shared")
    assert not sandbox_secret_matches("shared", None)
    assert not sandbox_secret_matches(None, "shared")


def test_client_annotations_cannot_forge_the_internal_sandbox_marker():
    annotations = build_run_annotations(
        {"tester": {"source": "workflow_editor"}},
        {"sysevo_test_sandbox": {"enabled": True}, "scenario_id": "qa"},
        sandbox_authorized=False,
    )

    assert "sysevo_test_sandbox" not in annotations
    assert annotations["scenario_id"] == "qa"


def test_trusted_test_session_gets_a_server_owned_marker():
    annotations = build_run_annotations(
        {"tester": {"source": "workflow_editor"}},
        {
            "sysevo_test_sandbox": {"enabled": False},
            "agent_test_run_id": "12345678-1234-4234-8234-123456789abc",
            "scenario_id": "sandbox_calendar_booking",
        },
        sandbox_authorized=True,
    )

    assert is_sandbox_test_run(SimpleNamespace(annotations=annotations))
    assert annotations["sysevo_test_sandbox"] == {
        "enabled": True,
        "source": "agent_testing_engine",
        "agent_test_run_id": "12345678-1234-4234-8234-123456789abc",
        "scenario_id": "sandbox_calendar_booking",
    }


def test_enabled_server_marker_without_valid_action_context_still_fails_closed():
    run = SimpleNamespace(annotations={"sysevo_test_sandbox": {"enabled": True}})
    assert is_sandbox_test_run(run)
    assert sandbox_post_call_policy(run) == {
        "status": "blocked",
        "reason": "sandbox_marker_has_incomplete_run_identity",
        "agent_test_run_id": None,
        "scenario_id": None,
        "blocked_surfaces": [
            "workflow_webhooks",
            "completion_handlers",
            "caller_memory",
            "wallet_debit",
            "client_live_call_notifications",
        ],
    }


def test_valid_sandbox_context_blocks_post_call_surfaces_without_customer_data():
    annotations = build_run_annotations(
        {},
        {
            "agent_test_run_id": "12345678-1234-4234-8234-123456789abc",
            "scenario_id": "calendar-booking",
        },
        sandbox_authorized=True,
    )

    policy = sandbox_post_call_policy(SimpleNamespace(annotations=annotations))

    assert policy == {
        "status": "blocked",
        "reason": "post_call_side_effects_are_disabled_for_agent_tests",
        "agent_test_run_id": "12345678-1234-4234-8234-123456789abc",
        "scenario_id": "calendar-booking",
        "blocked_surfaces": [
            "workflow_webhooks",
            "completion_handlers",
            "caller_memory",
            "wallet_debit",
            "client_live_call_notifications",
        ],
    }


def test_malformed_sandbox_run_id_still_blocks_side_effects():
    run = SimpleNamespace(
        annotations={
            "sysevo_test_sandbox": {
                "enabled": True,
                "agent_test_run_id": "x" * 36,
                "scenario_id": "scenario",
            }
        }
    )
    assert is_sandbox_test_run(run)
    assert sandbox_post_call_policy(run)["reason"] == "sandbox_marker_has_incomplete_run_identity"


@pytest.mark.asyncio
async def test_live_call_webhook_blocks_any_server_sandbox_marker(monkeypatch):
    from api.services import call_live_webhook

    monkeypatch.setenv("SYSEVO_CALL_LIVE_URL", "https://sysevo.test/live")
    run = SimpleNamespace(
        annotations={"sysevo_test_sandbox": {"enabled": True}}
    )
    with (
        patch.object(
            call_live_webhook.db_client,
            "get_workflow_run_by_id",
            new=AsyncMock(return_value=run),
        ),
        patch.object(call_live_webhook.httpx, "AsyncClient") as http_client,
    ):
        await call_live_webhook.fire_call_live("started", 123)

    http_client.assert_not_called()
