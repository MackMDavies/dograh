"""Mocked contract tests for the catalogue used by prospect and provisioning builds."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import user as routes
from api.services.auth.depends import get_user
from api.services.voice_library import elevenlabs_service as service


class ConfigurationVoiceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.user = NS(id=7, provider_id="test-provider", selected_organization_id=42)
        app = FastAPI()
        app.include_router(routes.router, prefix="/api/v1")
        app.dependency_overrides[get_user] = lambda: self.user
        self.client = TestClient(app)
        self.caller = self.enterContext(patch.object(service, "get_caller_elevenlabs_api_key", AsyncMock(return_value="caller-key")))
        self.org = self.enterContext(patch.object(service.db_client, "get_connection_by_provider", AsyncMock(return_value=None)))
        self.system = self.enterContext(patch.object(service, "get_system_elevenlabs_api_key", AsyncMock(return_value="platform-key")))
        self.catalog = self.enterContext(patch.object(service, "fetch_elevenlabs_catalog", AsyncMock(return_value=[{
            "voice_id": "voice-123", "name": "Sarah - Warm", "description": "Friendly",
            "preview_url": "https://example.com/voice.mp3",
            "labels": {"accent": "american", "gender": "female", "language": "en"},
        }])))
        self.proxy = self.enterContext(patch.object(routes.mps_service_key_client, "get_voices", AsyncMock(side_effect=RuntimeError("Unsupported provider: elevenlabs"))))

    def request(self):
        return self.client.get("/api/v1/user/configurations/voices/elevenlabs")

    def test_elevenlabs_uses_direct_catalog_and_preserves_builder_response(self):
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"provider": "elevenlabs", "voices": [{
            "voice_id": "voice-123", "name": "Sarah - Warm", "description": "Friendly",
            "accent": "american", "gender": "female", "language": "en",
            "preview_url": "https://example.com/voice.mp3",
        }]})
        self.catalog.assert_awaited_once_with("caller-key")
        self.proxy.assert_not_awaited()
        self.org.assert_not_awaited()
        self.system.assert_not_awaited()

    def test_organization_key_is_scoped_to_authenticated_organization(self):
        self.caller.return_value = None
        self.org.return_value = NS(api_key="org-key")
        self.assertEqual(self.request().status_code, 200)
        self.org.assert_awaited_once_with(42, "tts", "elevenlabs")
        self.catalog.assert_awaited_once_with("org-key")
        self.system.assert_not_awaited()

    def test_platform_key_supports_managed_demo_organizations(self):
        self.caller.return_value = None
        self.assertEqual(self.request().status_code, 200)
        self.catalog.assert_awaited_once_with("platform-key")

    def test_no_organization_does_not_query_an_unscoped_connection(self):
        self.caller.return_value = None
        self.user.selected_organization_id = None
        self.assertEqual(self.request().status_code, 200)
        self.org.assert_not_awaited()
        self.catalog.assert_awaited_once_with("platform-key")

    def test_missing_key_is_actionable_and_does_not_call_proxy(self):
        self.caller.return_value = self.system.return_value = None
        response = self.request()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("ElevenLabs API key", response.json()["detail"])
        self.proxy.assert_not_awaited()
        self.catalog.assert_not_awaited()

    def test_provider_failure_does_not_expose_credentials(self):
        self.catalog.side_effect = RuntimeError("private-provider-error caller-key")
        response = self.request()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("caller-key", response.text)
        self.proxy.assert_not_awaited()

    def test_other_providers_keep_existing_proxy_and_filters(self):
        self.proxy.side_effect = None
        self.proxy.return_value = {"provider": "cartesia", "voices": []}
        response = self.client.get("/api/v1/user/configurations/voices/cartesia?model=sonic&language=en")
        self.assertEqual(response.status_code, 200)
        self.proxy.assert_awaited_once_with(provider="cartesia", model="sonic", language="en", organization_id=42, created_by="test-provider")
        self.catalog.assert_not_awaited()

    def test_optional_labels_can_be_absent(self):
        self.catalog.return_value = [{"voice_id": "v1", "name": "Alice", "labels": None}]
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["voices"][0]["accent"])


if __name__ == "__main__":
    unittest.main()
