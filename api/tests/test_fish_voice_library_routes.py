from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import app

client = TestClient(app)


def _auth_headers():
    return {"Authorization": "Bearer test-token"}


class TestFishCatalogEndpoint:
    @pytest.mark.asyncio
    async def test_get_fish_voices_requires_configured_key(self, monkeypatch):
        from api.services.auth.depends import get_user

        async def fake_user():
            from types import SimpleNamespace

            return SimpleNamespace(id=1, selected_organization_id=1, is_superuser=False)

        app.dependency_overrides[get_user] = fake_user
        with (
            patch(
                "api.routes.voice_library.get_caller_fish_api_key",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "api.routes.voice_library.db_client.get_connection_by_provider",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "api.routes.voice_library.get_system_fish_api_key",
                new=AsyncMock(return_value=None),
            ),
        ):
            response = client.get("/api/v1/voice-library/fish/voices", headers=_auth_headers())
        app.dependency_overrides.pop(get_user, None)
        assert response.status_code == 400
        assert "Fish Audio" in response.json()["detail"]
