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
        is_superuser=False,
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
    mock_config = SimpleNamespace(
        id=55, provider="twilio", name="Sysevo Managed",
        credentials={"account_sid": "ACplatform", "auth_token": "tok"},
    )
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
        instance.account_sid, instance.auth_token = "ACplatform", "tok"
        instance.regulation_for.return_value = {"sid": None, "requires_bundle": False}
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
        extra_metadata={"is_managed": True, "managed_twilio_sid": "PN999"},
        telephony_configuration_id=55,
    )

    with (
        patch("api.routes.telephony_quick_connect.db_client") as mock_db,
        patch("api.routes.telephony_quick_connect.get_managed_provisioner", new_callable=AsyncMock) as mock_p,
    ):
        # The release uses the config that OWNS the number; this one stored no
        # credentials, so it falls back to the active platform account.
        mock_db.get_telephony_configuration = AsyncMock(return_value=SimpleNamespace(credentials={}))
        mock_db.get_phone_number = AsyncMock(return_value=mock_row)
        mock_db.delete_phone_number = AsyncMock(return_value=True)

        instance = MagicMock()
        instance.release_number.return_value = True
        mock_p.return_value = instance

        resp = client.delete("/telephony/managed-numbers/42")

    assert resp.status_code == 204
    instance.release_number.assert_called_once_with("PN999")


# ── regulated countries: the client's own bundle, never a silent US number ────────

def _gb_setup(instance):
    instance.account_sid, instance.auth_token = "ACplatform", "tok"
    instance.search_available_numbers.return_value = ["+442071234567"]
    instance.provision_number.return_value = ProvisionedNumber(e164="+442071234567", twilio_sid="PNgb")


def _post_quick_connect(instance, body):
    app = _make_test_app()
    client = TestClient(app)
    mock_config = SimpleNamespace(id=55, name="Sysevo Managed", credentials={"account_sid": "ACplatform"})
    with (
        patch("api.routes.telephony_quick_connect.get_managed_provisioner", new=AsyncMock(return_value=instance)),
        patch("api.routes.telephony_quick_connect.db_client") as mock_db,
        patch(
            "api.routes.telephony_quick_connect.get_backend_endpoints",
            new=AsyncMock(return_value=("https://api.example.com", "wss://ignored")),
        ),
    ):
        mock_db.list_telephony_configurations = AsyncMock(return_value=[mock_config])
        mock_db.create_phone_number = AsyncMock(return_value=SimpleNamespace(id=99))
        return client.post("/telephony/quick-connect", json=body)


def test_quick_connect_refuses_a_regulated_country_without_a_bundle_before_buying():
    instance = MagicMock()
    _gb_setup(instance)
    instance.regulation_for.return_value = {"sid": "RNgb", "requires_bundle": True}

    resp = _post_quick_connect(instance, {"mode": "new", "country": "GB"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "regulatory_bundle_required"
    instance.provision_number.assert_not_called()


def test_quick_connect_buys_in_country_against_the_clients_approved_bundle():
    instance = MagicMock()
    _gb_setup(instance)
    instance.get_bundle.return_value = {"status": "twilio-approved", "iso_country": "GB"}

    resp = _post_quick_connect(
        instance, {"mode": "new", "country": "GB", "bundle_sid": "BUclient", "address_sid": "ADclient"},
    )

    assert resp.status_code == 200
    assert resp.json()["managed_number"] == "+442071234567"
    args = instance.provision_number.call_args.args
    assert args[2:] == ("ADclient", "BUclient")
    # Never the platform-wide lookup that could hand client A's bundle to client B.
    instance.get_bundle_sid.assert_not_called()
    instance.get_address_sid.assert_not_called()


def test_quick_connect_waits_for_a_bundle_twilio_has_not_approved():
    instance = MagicMock()
    _gb_setup(instance)
    instance.get_bundle.return_value = {"status": "in-review", "iso_country": "GB"}

    resp = _post_quick_connect(instance, {"mode": "new", "country": "GB", "bundle_sid": "BUclient"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "regulatory_bundle_not_approved"
    instance.provision_number.assert_not_called()


def test_quick_connect_never_falls_back_to_a_us_number():
    from twilio.base.exceptions import TwilioRestException

    instance = MagicMock()
    _gb_setup(instance)
    instance.regulation_for.return_value = {"sid": None, "requires_bundle": False}
    instance.provision_number.side_effect = TwilioRestException(
        400, "https://api.twilio.com", msg="Phone Number Requires an Address and Bundle"
    )

    resp = _post_quick_connect(instance, {"mode": "new", "country": "GB"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "regulatory_bundle_required"
    searched = [c.args[0] for c in instance.search_available_numbers.call_args_list]
    assert "US" not in searched
    assert instance.provision_number.call_count == 1


def test_create_bundle_names_missing_kyc_fields():
    app = _make_test_app()
    client = TestClient(app)
    instance = MagicMock()
    instance.create_bundle.side_effect = ValueError("KYC is missing fields GB requires: business_registration_number")
    with patch("api.routes.telephony_quick_connect.get_managed_provisioner", new=AsyncMock(return_value=instance)):
        resp = client.post("/telephony/regulatory/bundles", json={"country": "GB", "kyc": {}, "email": "ops@x.io"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "kyc_incomplete"
    assert "business_registration_number" in resp.json()["detail"]["message"]
