from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import workflow_text_chat
from api.routes.workflow_text_chat import router as text_chat_router
from api.services.auth.depends import get_user
from api.services.workflow.test_sandbox_binding import (
    validate_sandbox_execution_binding,
)
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


def test_candidate_hash_mismatch_is_rejected_before_quota_or_runtime_creation(
    monkeypatch,
):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "synthetic-test-secret")
    app = FastAPI()
    app.include_router(text_chat_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1, selected_organization_id=11
    )
    candidate = SimpleNamespace(
        workflow_id=42,
        version_number=4,
        status="draft",
        workflow_json={"nodes": []},
        workflow_configurations={},
        template_context_variables={},
    )
    with (
        patch.object(
            workflow_text_chat.db_client,
            "get_workflow",
            new=AsyncMock(return_value=SimpleNamespace(id=42)),
        ),
        patch.object(
            workflow_text_chat.db_client,
            "get_workflow_versions",
            new=AsyncMock(return_value=[candidate]),
        ),
        patch.object(
            workflow_text_chat, "_ensure_text_chat_quota", new=AsyncMock()
        ) as quota,
        patch.object(
            workflow_text_chat.db_client, "create_workflow_run", new=AsyncMock()
        ) as create,
    ):
        response = TestClient(app).post(
            "/workflow/42/text-chat/sessions",
            json={
                "sandbox_mode": True,
                "candidate_draft_version_number": 4,
                "expected_workflow_hash": "0" * 64,
                "annotations": {
                    "agent_test_run_id": "11111111-1111-4111-8111-111111111111",
                    "scenario_id": "qa",
                },
            },
            headers={"x-sysevo-secret": "synthetic-test-secret"},
        )
    assert response.status_code == 409
    quota.assert_not_awaited()
    create.assert_not_awaited()


def test_runtime_response_contains_server_signed_binding_instead_of_caller_marker(
    monkeypatch,
):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "synthetic-test-secret")
    app = FastAPI()
    app.include_router(text_chat_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1, selected_organization_id=11
    )
    candidate = SimpleNamespace(
        workflow_id=42,
        version_number=4,
        status="draft",
        workflow_json={"nodes": []},
        workflow_configurations={},
        template_context_variables={},
    )
    run = SimpleNamespace(
        id=7,
        workflow_id=42,
        name="qa",
        mode="text_chat",
        state="running",
        is_completed=False,
        initial_context={},
        gathered_context={},
        annotations={},
    )
    text_session = SimpleNamespace(
        workflow_run=run,
        revision=1,
        session_data={},
        checkpoint={},
        created_at=datetime.now(UTC),
        updated_at=None,
    )

    async def annotate(_run_id, *, annotations):
        run.annotations = annotations
        return run

    with (
        patch.object(
            workflow_text_chat.db_client,
            "get_workflow",
            new=AsyncMock(return_value=SimpleNamespace(id=42)),
        ),
        patch.object(
            workflow_text_chat.db_client,
            "get_workflow_versions",
            new=AsyncMock(return_value=[candidate]),
        ),
        patch.object(workflow_text_chat, "_ensure_text_chat_quota", new=AsyncMock()),
        patch.object(
            workflow_text_chat.db_client,
            "create_workflow_run",
            new=AsyncMock(return_value=run),
        ),
        patch.object(
            workflow_text_chat.db_client,
            "update_workflow_run",
            new=AsyncMock(side_effect=annotate),
        ),
        patch.object(
            workflow_text_chat.db_client,
            "ensure_workflow_run_text_session",
            new=AsyncMock(return_value=text_session),
        ),
        patch.object(
            workflow_text_chat,
            "initialize_text_chat_session",
            new=AsyncMock(return_value=text_session),
        ),
        patch.object(
            workflow_text_chat,
            "_execute_pending_turn_response",
            new=AsyncMock(
                side_effect=lambda **_: workflow_text_chat._build_response(text_session)
            ),
        ),
    ):
        response = TestClient(app).post(
            "/workflow/42/text-chat/sessions",
            json={
                "sandbox_mode": True,
                "candidate_draft_version_number": 4,
                "expected_workflow_hash": "3a6c5731f42967f3150ec663339ec161bf6f849595fe2bcea0127693ff5c194b",
                "annotations": {
                    "agent_test_run_id": "11111111-1111-4111-8111-111111111111",
                    "scenario_id": "qa",
                    "sysevo_test_sandbox": {
                        "enabled": False,
                        "workflow_binding": {"signature": "forged"},
                    },
                },
            },
            headers={"x-sysevo-secret": "synthetic-test-secret"},
        )
    assert response.status_code == 200
    binding = response.json()["annotations"]["sysevo_test_sandbox"]["workflow_binding"]
    assert (
        binding["signature"]
        == "3cd799beea0f2a6a421a64752cddc72d31dc4162a9b50c3a3e9efb754fd591a6"
    )
    validate_sandbox_execution_binding(run, candidate, "synthetic-test-secret")


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
