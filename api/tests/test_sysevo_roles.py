"""Tests for the Sysevo role-lookup gate used by internal-staff-only routes.

Dograh's own UserModel has no `role` concept — Sysevo roles (sales_rep,
sales_manager, client, ...) live entirely in Supabase's `user_roles` table.
`require_sales_dialer_role` resolves them live over Supabase's REST API
(anon key + the caller's own bearer token) and 403s anyone who isn't a
superuser or a sales_rep/sales_manager/super_admin.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import HTTPException

from api.services.auth.sysevo_roles import (
    get_sysevo_roles,
    require_sales_dialer_role,
)


def _mock_role_response(roles: list[str]):
    mock_client = AsyncMock()
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = [{"role": role} for role in roles]
    mock_client.get.return_value = mock_response
    return mock_client


async def test_get_sysevo_roles_returns_roles_from_response(monkeypatch):
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_ANON_KEY", "test-anon-key"
    )

    mock_client = _mock_role_response(["sales_rep", "client"])
    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        roles = await get_sysevo_roles("user-uuid-123", "some-access-token")

        assert roles == ["sales_rep", "client"]
        call_kwargs = mock_client.get.call_args.kwargs
        assert call_kwargs["params"]["user_id"] == "eq.user-uuid-123"
        assert call_kwargs["headers"]["apikey"] == "test-anon-key"
        assert call_kwargs["headers"]["Authorization"] == "Bearer some-access-token"


async def test_require_sales_dialer_role_allows_sales_rep(monkeypatch):
    monkeypatch.setattr("api.services.auth.sysevo_roles.AUTH_PROVIDER", "supabase")
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_ANON_KEY", "test-anon-key"
    )

    user = SimpleNamespace(id=1, provider_id="user-uuid-123", is_superuser=False)
    mock_client = _mock_role_response(["sales_rep"])

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await require_sales_dialer_role(
            authorization="Bearer some-access-token", user=user
        )

        assert result is user


async def test_require_sales_dialer_role_allows_superuser_without_http_call(
    monkeypatch,
):
    monkeypatch.setattr("api.services.auth.sysevo_roles.AUTH_PROVIDER", "supabase")
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_ANON_KEY", "test-anon-key"
    )

    user = SimpleNamespace(id=1, provider_id="user-uuid-123", is_superuser=True)

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        result = await require_sales_dialer_role(
            authorization="Bearer some-access-token", user=user
        )

        assert result is user
        mock_client_class.assert_not_called()


async def test_require_sales_dialer_role_rejects_client_role(monkeypatch):
    monkeypatch.setattr("api.services.auth.sysevo_roles.AUTH_PROVIDER", "supabase")
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_ANON_KEY", "test-anon-key"
    )

    user = SimpleNamespace(id=1, provider_id="user-uuid-123", is_superuser=False)
    mock_client = _mock_role_response(["client"])

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with pytest.raises(HTTPException) as exc_info:
            await require_sales_dialer_role(
                authorization="Bearer some-access-token", user=user
            )

        assert exc_info.value.status_code == 403


async def test_require_sales_dialer_role_rejects_non_supabase_auth_provider(
    monkeypatch,
):
    monkeypatch.setattr("api.services.auth.sysevo_roles.AUTH_PROVIDER", "local")

    user = SimpleNamespace(id=1, provider_id="user-uuid-123", is_superuser=False)

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        with pytest.raises(HTTPException) as exc_info:
            await require_sales_dialer_role(
                authorization="Bearer some-access-token", user=user
            )

        assert exc_info.value.status_code == 403
        mock_client_class.assert_not_called()


async def test_require_sales_dialer_role_rejects_missing_authorization_header(
    monkeypatch,
):
    monkeypatch.setattr("api.services.auth.sysevo_roles.AUTH_PROVIDER", "supabase")

    user = SimpleNamespace(id=1, provider_id="user-uuid-123", is_superuser=False)

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        with pytest.raises(HTTPException) as exc_info:
            await require_sales_dialer_role(authorization=None, user=user)

        assert exc_info.value.status_code == 403
        mock_client_class.assert_not_called()


async def test_get_sysevo_roles_fails_closed_on_supabase_error(monkeypatch):
    """A Supabase outage/timeout must deny access (503), never be silently
    treated as "checked, found no roles" — that would fail open."""
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_ANON_KEY", "test-anon-key"
    )

    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.ConnectTimeout("timed out")

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with pytest.raises(HTTPException) as exc_info:
            await get_sysevo_roles("user-uuid-123", "some-access-token")

        assert exc_info.value.status_code == 503


async def test_require_sales_dialer_role_fails_closed_when_role_lookup_errors(
    monkeypatch,
):
    """The dependency itself must not swallow a failed role lookup and let
    the user through — it should propagate as a denial, not a pass."""
    monkeypatch.setattr("api.services.auth.sysevo_roles.AUTH_PROVIDER", "supabase")
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setattr(
        "api.services.auth.sysevo_roles.SUPABASE_ANON_KEY", "test-anon-key"
    )

    user = SimpleNamespace(id=1, provider_id="user-uuid-123", is_superuser=False)

    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.ConnectTimeout("timed out")

    with patch(
        "api.services.auth.sysevo_roles.httpx.AsyncClient"
    ) as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with pytest.raises(HTTPException) as exc_info:
            await require_sales_dialer_role(
                authorization="Bearer some-access-token", user=user
            )

        assert exc_info.value.status_code == 503
