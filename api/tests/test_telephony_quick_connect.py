"""Tests for Quick Connect endpoints — Twilio calls mocked."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.telephony_quick_connect import router
from api.services.auth.depends import get_user
from api.services.telephony.managed_provisioner import ProvisionedNumber


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=7,
        selected_organization_id=11,
    )
    return app


# ── carrier-lookup ────────────────────────────────────────────────────────────

def test_carrier_lookup_returns_detected_carrier():
    app = _make_test_app()
    client = TestClient(app)

    mock_result = {"carrier": "EE", "country": "GB", "line_type": "mobile"}
    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock) as mock_p:
        instance = MagicMock()
        instance.lookup_carrier.return_value = mock_result
        mock_p.return_value = instance

        resp = client.get("/telephony/carrier-lookup?number=%2B447700900123")

    assert resp.status_code == 200
    assert resp.json()["carrier"] == "EE"
    assert resp.json()["country"] == "GB"
    assert resp.json()["line_type"] == "mobile"


def test_carrier_lookup_503_when_not_configured():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock, return_value=None):
        resp = client.get("/telephony/carrier-lookup?number=%2B447700900123")

    assert resp.status_code == 503


# ── available-numbers ─────────────────────────────────────────────────────────

def test_available_numbers_returns_list():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock) as mock_p:
        instance = MagicMock()
        instance.search_available_numbers.return_value = ["+12125551234", "+12125555678"]
        mock_p.return_value = instance

        resp = client.get("/telephony/available-numbers?country=US")

    assert resp.status_code == 200
    assert resp.json()["numbers"] == ["+12125551234", "+12125555678"]


def test_available_numbers_503_when_not_configured():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock, return_value=None):
        resp = client.get("/telephony/available-numbers?country=US")

    assert resp.status_code == 503


# ── quick-connect ─────────────────────────────────────────────────────────────

def test_quick_connect_forward_mode_requires_existing_number():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock) as mock_p:
        mock_p.return_value = MagicMock()
        resp = client.post(
            "/telephony/quick-connect",
            json={"mode": "forward", "country": "GB"},
        )

    assert resp.status_code == 422


def test_quick_connect_503_when_not_configured():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock, return_value=None):
        resp = client.post(
            "/telephony/quick-connect",
            json={"mode": "new", "country": "US"},
        )

    assert resp.status_code == 503


def test_quick_connect_new_mode_provisions_and_returns_ids():
    app = _make_test_app()
    client = TestClient(app)

    provisioned = ProvisionedNumber(e164="+12125551234", twilio_sid="PN123")
    mock_config = SimpleNamespace(id=55, provider="twilio", name="Sysevo Managed")
    mock_phone = SimpleNamespace(id=99)

    with (
        patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock) as mock_p,
        patch("api.routes.telephony_quick_connect.db_client") as mock_db,
        patch(
            "api.routes.telephony_quick_connect.get_backend_endpoints",
            new=AsyncMock(return_value=("https://api.example.com", "wss://ignored")),
        ),
    ):
        instance = MagicMock()
        instance.search_available_numbers.return_value = ["+12125551234"]
        instance.provision_number.return_value = provisioned
        mock_p.return_value = instance

        mock_db.list_telephony_configurations = AsyncMock(return_value=[mock_config])
        mock_db.create_phone_number = AsyncMock(return_value=mock_phone)

        resp = client.post(
            "/telephony/quick-connect",
            json={"mode": "new", "country": "US"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["managed_number"] == "+12125551234"
    assert data["telephony_config_id"] == 55
    assert data["phone_number_id"] == 99


# ── delete managed-numbers ────────────────────────────────────────────────────

def test_delete_managed_number_404_when_not_found():
    app = _make_test_app()
    client = TestClient(app)

    with patch("api.routes.telephony_quick_connect.db_client") as mock_db:
        mock_db.get_phone_number = AsyncMock(return_value=None)
        resp = client.delete("/telephony/managed-numbers/999")

    assert resp.status_code == 404


def test_delete_managed_number_400_when_not_managed():
    app = _make_test_app()
    client = TestClient(app)

    mock_row = SimpleNamespace(extra_metadata={"is_managed": False})
    with patch("api.routes.telephony_quick_connect.db_client") as mock_db:
        mock_db.get_phone_number = AsyncMock(return_value=mock_row)
        resp = client.delete("/telephony/managed-numbers/42")

    assert resp.status_code == 400


def test_delete_managed_number_releases_twilio_and_returns_204():
    app = _make_test_app()
    client = TestClient(app)

    mock_row = SimpleNamespace(
        extra_metadata={"is_managed": True, "managed_twilio_sid": "PN999"}
    )

    with (
        patch("api.routes.telephony_quick_connect.db_client") as mock_db,
        patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock) as mock_p,
    ):
        mock_db.get_phone_number = AsyncMock(return_value=mock_row)
        mock_db.delete_phone_number = AsyncMock(return_value=True)

        instance = MagicMock()
        instance.release_number.return_value = True
        mock_p.return_value = instance

        resp = client.delete("/telephony/managed-numbers/42")

    assert resp.status_code == 204
    instance.release_number.assert_called_once_with("PN999")
