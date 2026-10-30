from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.voice_library import router
from api.services.auth.depends import get_user


def _make_app(is_superuser=False):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1,
        provider_id="provider-1",
        selected_organization_id=11,
        is_superuser=is_superuser,
    )
    return app


def _make_voice(uuid="v-abc", is_public=True, user_id=1, org_id=11):
    v = MagicMock()
    v.uuid = uuid
    v.user_id = user_id
    v.organization_id = org_id
    v.name = "Test Voice"
    v.description = None
    v.provider = "dograh_clone"
    v.provider_voice_id = "el-xyz"
    v.language = "en"
    v.accent = "american"
    v.gender = "female"
    v.age = "young"
    v.use_case = "conversation"
    v.is_public = is_public
    v.status = "ready"
    v.audio_preview_url = None
    v.labels = {}
    v.created_at = datetime.now(UTC)
    v.updated_at = datetime.now(UTC)
    return v


def test_list_voices_returns_200():
    app = _make_app()
    client = TestClient(app)
    mock_voice = _make_voice()

    with patch("api.routes.voice_library.db_client") as mock_db:
        mock_db.get_platform_organization_id = AsyncMock(return_value=11)
        mock_db.list_voices = AsyncMock(return_value=[mock_voice])
        response = client.get("/voice-library")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert data[0]["uuid"] == "v-abc"


def test_list_voices_supports_paged_search_and_filters():
    app = _make_app(is_superuser=True)
    client = TestClient(app)

    with patch("api.routes.voice_library.db_client") as mock_db:
        mock_db.list_voices = AsyncMock(return_value=[_make_voice()])
        response = client.get(
            "/voice-library",
            params={"limit": 60, "offset": 120, "q": "British", "gender": "Female", "accent": "British", "provider": "elevenlabs"},
        )

    assert response.status_code == 200
    mock_db.list_voices.assert_awaited_once_with(
        organization_id=11,
        user_id=1,
        is_superuser=True,
        language=None,
        gender="female",
        age=None,
        use_case=None,
        provider="elevenlabs",
        status=None,
        q="British",
        accent="British",
        limit=60,
        offset=120,
    )


def test_delete_voice_returns_204():
    app = _make_app()
    client = TestClient(app)

    with patch("api.routes.voice_library.db_client") as mock_db:
        mock_db.delete_voice = AsyncMock(return_value=True)
        response = client.delete("/voice-library/v-abc")

    assert response.status_code == 204


def test_elevenlabs_catalog_uses_platform_key_for_regular_user_without_own_key():
    app = _make_app(is_superuser=False)
    client = TestClient(app)

    with patch("api.routes.voice_library.db_client") as mock_db:
        mock_db.get_connection_by_provider = AsyncMock(return_value=None)
        mock_db.list_all_connections_superuser = AsyncMock(return_value=[])
        with patch(
            "api.routes.voice_library.get_caller_elevenlabs_api_key",
            new=AsyncMock(return_value=None),
        ):
            with patch(
                "api.routes.voice_library.get_system_elevenlabs_api_key",
                new=AsyncMock(return_value="platform-key"),
            ):
                with patch(
                    "api.routes.voice_library.fetch_elevenlabs_catalog",
                    new=AsyncMock(return_value=[
                        {
                            "voice_id": "v1",
                            "name": "Rachel",
                            "preview_url": None,
                            "labels": {},
                            "category": "premade",
                        }
                    ]),
                ) as fetch_catalog:
                    response = client.get("/voice-library/elevenlabs/voices")

    assert response.status_code == 200
    fetch_catalog.assert_awaited_once_with("platform-key")
    mock_db.list_all_connections_superuser.assert_not_awaited()


def test_elevenlabs_catalog_allowed_for_superuser():
    app = _make_app(is_superuser=True)
    client = TestClient(app)

    with patch("api.routes.voice_library.db_client"):
        with patch("api.routes.voice_library.get_caller_elevenlabs_api_key", new=AsyncMock(return_value="test-key")):
            with patch("api.routes.voice_library.fetch_elevenlabs_catalog", new=AsyncMock(return_value=[
                {"voice_id": "v1", "name": "Rachel", "preview_url": None, "labels": {}, "category": "premade"}
            ])):
                response = client.get("/voice-library/elevenlabs/voices")

    assert response.status_code == 200
    assert response.json()[0]["voice_id"] == "v1"
