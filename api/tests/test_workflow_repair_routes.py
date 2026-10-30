from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def _user():
    return SimpleNamespace(is_superuser=False, selected_organization_id=91)


def test_test_repair_routes_are_registered_once():
    from api.routes.workflow import router

    prefix = router.prefix.rstrip("/")
    for path, method in (
        ("/{workflow_id}/create-test-repair-draft", "POST"),
        ("/{workflow_id}/promote-test-repair-draft", "POST"),
        ("/{workflow_id}/discard-draft", "DELETE"),
    ):
        matches = [
            route
            for route in router.routes
            if getattr(route, "path", None) == f"{prefix}{path}"
            and method in getattr(route, "methods", set())
        ]
        assert len(matches) == 1, f"{method} {path} must have one scoped handler"


@pytest.mark.asyncio
async def test_test_repair_requires_internal_secret_before_database_access(monkeypatch):
    from api.db import db_client
    from api.routes.workflow import PrepareTestRepairDraftRequest, create_test_repair_draft

    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "repair-secret")
    get_workflow = AsyncMock()
    monkeypatch.setattr(db_client, "get_workflow", get_workflow)
    request = PrepareTestRepairDraftRequest(
        expected_published_version_number=4,
        operation="remove_sandbox_markers",
    )

    with pytest.raises(HTTPException) as error:
        await create_test_repair_draft(
            12,
            request,
            user=_user(),
            x_sysevo_secret="wrong-secret",
        )

    assert error.value.status_code == 403
    get_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_repair_uses_tenant_scoped_workflow_and_bounded_db_operation(monkeypatch):
    from api.db import db_client
    from api.routes.workflow import PrepareTestRepairDraftRequest, create_test_repair_draft

    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "repair-secret")
    get_workflow = AsyncMock(return_value=SimpleNamespace(id=12))
    create_draft = AsyncMock(
        return_value=(SimpleNamespace(version_number=5), ["$.nodes[0].config"], "a" * 64)
    )
    monkeypatch.setattr(db_client, "get_workflow", get_workflow)
    monkeypatch.setattr(db_client, "create_sandbox_repair_draft", create_draft)
    request = PrepareTestRepairDraftRequest(
        expected_published_version_number=4,
        operation="remove_sandbox_markers",
    )

    result = await create_test_repair_draft(
        12,
        request,
        user=_user(),
        x_sysevo_secret="repair-secret",
    )

    get_workflow.assert_awaited_once_with(12, organization_id=91)
    create_draft.assert_awaited_once_with(12, 4, "remove_sandbox_markers")
    assert result == {
        "workflow_id": 12,
        "candidate_version_number": 5,
        "changed_paths": ["$.nodes[0].config"],
        "candidate_hash": "a" * 64,
        "status": "draft",
    }
