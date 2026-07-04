from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.organization_usage import router
from api.services.auth.depends import get_user


def _make_test_app(user: SimpleNamespace) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: user
    return app


def test_usage_runs_scopes_to_caller_organization_for_non_superuser():
    """A regular client-org user must only see their own organization's runs.

    Regression test for the Jun 24 change that hardcoded organization_id=None
    for every caller, leaking every organization's call runs (including caller
    phone numbers, transcripts, and recording URLs) to any authenticated user.
    """
    user = SimpleNamespace(id=1, is_superuser=False, selected_organization_id=42)
    app = _make_test_app(user)
    client = TestClient(app)

    with patch("api.routes.organization_usage.db_client") as mock_db:
        mock_db.get_usage_history = AsyncMock(return_value=([], 0, 0.0, 0))

        response = client.get("/organizations/usage/runs")

    assert response.status_code == 200
    mock_db.get_usage_history.assert_awaited_once()
    called_org_id = mock_db.get_usage_history.await_args.args[0]
    assert called_org_id == 42, (
        "Non-superuser callers must be scoped to their own organization_id, "
        f"got {called_org_id!r} instead"
    )


def test_usage_runs_allows_superuser_to_see_all_organizations():
    user = SimpleNamespace(id=1, is_superuser=True, selected_organization_id=None)
    app = _make_test_app(user)
    client = TestClient(app)

    with patch("api.routes.organization_usage.db_client") as mock_db:
        mock_db.get_usage_history = AsyncMock(return_value=([], 0, 0.0, 0))

        response = client.get("/organizations/usage/runs")

    assert response.status_code == 200
    mock_db.get_usage_history.assert_awaited_once()
    called_org_id = mock_db.get_usage_history.await_args.args[0]
    assert called_org_id is None
