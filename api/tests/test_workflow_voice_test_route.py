from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import workflow_voice_test
from api.services.auth.depends import get_user
from api.services.workflow.voice_test_control import VoiceControlError
from api.tests.test_voice_test_control import LEASE, SECRET, SESSION


def setup(monkeypatch):
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    user = SimpleNamespace(id=9, selected_organization_id=42)
    app = FastAPI()
    app.include_router(workflow_voice_test.router)
    app.dependency_overrides[get_user] = lambda: user
    from api.tasks import arq

    pool = AsyncMock()
    monkeypatch.setattr(arq, "get_arq_redis", pool)
    enqueue = AsyncMock(return_value={"status": "queued", "session_id": SESSION})
    monkeypatch.setattr(workflow_voice_test, "enqueue_voice_job", enqueue)
    return TestClient(app), user, pool, enqueue


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_voice_dispatch_requires_internal_secret_before_queue_access(
    monkeypatch, secret
):
    client, _, pool, enqueue = setup(monkeypatch)
    response = client.post(
        "/workflow/185/test-voice/sessions",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": secret} if secret else {},
    )
    assert response.status_code == 403
    pool.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_voice_dispatch_requires_authenticated_user(monkeypatch):
    client, _, pool, enqueue = setup(monkeypatch)
    client.app.dependency_overrides.clear()
    response = client.post(
        "/workflow/185/test-voice/sessions",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code in {401, 403}
    pool.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_voice_dispatch_uses_authenticated_scope_and_returns_no_lease(monkeypatch):
    client, _, pool, enqueue = setup(monkeypatch)
    response = client.post(
        "/workflow/185/test-voice/sessions",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 202 and response.json() == {
        "status": "queued",
        "session_id": SESSION,
    }
    context = enqueue.await_args.args[3]
    assert (
        context.organization_id == 42
        and context.requested_by_user_id == 9
        and context.workflow_id == 185
    )
    assert context.lease_token == LEASE
    assert LEASE not in response.text and SECRET not in response.text
    pool.assert_awaited_once()


@pytest.mark.parametrize(
    "extra",
    [
        {"workflow_id": 186},
        {"organization_id": 43},
        {"url": "https://client.invalid"},
        {"recipient": "client@example.invalid"},
        {"session_id": "../bad"},
        {"lease_token": []},
        {"session_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
    ],
)
def test_dispatch_rejects_untrusted_scope_recipients_and_invalid_identities(
    monkeypatch, extra
):
    client, _, pool, enqueue = setup(monkeypatch)
    response = client.post(
        "/workflow/185/test-voice/sessions",
        json={"session_id": SESSION, "lease_token": LEASE, **extra},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 422
    pool.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status",
    [
        (VoiceControlError("voice_workflow_not_found", 404), 404),
        (VoiceControlError("voice_queue_unavailable", 503), 503),
        (RuntimeError("private connection details"), 503),
    ],
)
def test_dispatch_errors_do_not_expose_credentials_or_internal_connection_errors(
    monkeypatch, error, status
):
    client, _, _, enqueue = setup(monkeypatch)
    enqueue.side_effect = error
    response = client.post(
        "/workflow/185/test-voice/sessions",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == status
    assert "private connection details" not in response.text
    assert LEASE not in response.text and SECRET not in response.text


def test_missing_organization_cannot_enqueue_a_voice_job(monkeypatch):
    client, user, pool, enqueue = setup(monkeypatch)
    user.selected_organization_id = None
    response = client.post(
        "/workflow/185/test-voice/sessions",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 400
    pool.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_status_uses_the_same_authentication_scope_and_keeps_rehearsal_quality_explicit(
    monkeypatch,
):
    client, _, _, enqueue = setup(monkeypatch)
    status = AsyncMock(
        return_value={
            "status": "completed",
            "report_status": "verified",
            "quality_evaluated": False,
        }
    )
    monkeypatch.setattr(workflow_voice_test, "read_voice_job_status", status)
    response = client.post(
        "/workflow/185/test-voice/status",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 200 and response.json()["quality_evaluated"] is False
    assert status.await_args.args[3].organization_id == 42
    enqueue.assert_not_awaited()


def test_status_without_internal_authorization_cannot_read_a_report(monkeypatch):
    client, _, pool, _ = setup(monkeypatch)
    status = AsyncMock()
    monkeypatch.setattr(workflow_voice_test, "read_voice_job_status", status)
    response = client.post(
        "/workflow/185/test-voice/status",
        json={"session_id": SESSION, "lease_token": LEASE},
    )
    assert response.status_code == 403
    status.assert_not_awaited()
    pool.assert_not_awaited()
