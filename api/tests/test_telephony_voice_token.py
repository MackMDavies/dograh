from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.providers.twilio.routes import router


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(
        id=42,
        selected_organization_id=11,
        is_superuser=False,
    )
    return app


def test_voice_token_returns_jwt_for_authenticated_user(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_ACCOUNT_SID", "AC" + "x" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SK" + "x" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("TWILIO_TWIML_APP_SID", "AP" + "x" * 32)

    client = TestClient(_make_test_app())
    response = client.get("/voice-token")

    assert response.status_code == 200
    body = response.json()
    assert body["identity"] == "rep-42"
    assert len(body["token"].split(".")) == 3


def test_voice_token_returns_503_when_not_configured(monkeypatch):
    for var in (
        "SYSEVO_TWILIO_ACCOUNT_SID",
        "TWILIO_API_KEY_SID",
        "TWILIO_API_KEY_SECRET",
        "TWILIO_TWIML_APP_SID",
    ):
        monkeypatch.delenv(var, raising=False)

    client = TestClient(_make_test_app())
    response = client.get("/voice-token")

    assert response.status_code == 503
