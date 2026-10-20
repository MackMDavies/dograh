import asyncio
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


def intent_request():
    return {
        "workflow_version": 3,
        "version_kind": "published",
        "runtime_workflow_hash": "a" * 64,
    }


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_provider_intent_requires_internal_authentication_before_configuration_reads(
    monkeypatch, secret
):
    client, _, _, _ = setup(monkeypatch)
    capture = AsyncMock()
    monkeypatch.setattr(workflow_voice_test, "capture_voice_provider_intent", capture)
    response = client.post(
        "/workflow/185/test-voice/provider-intent",
        json=intent_request(),
        headers={"x-sysevo-secret": secret} if secret else {},
    )
    assert response.status_code == 403
    capture.assert_not_awaited()


def test_provider_intent_requires_user_authentication_and_uses_server_scope(
    monkeypatch,
):
    client, _, pool, enqueue = setup(monkeypatch)
    capture = AsyncMock(return_value={"configuration_hash": "b" * 64})
    monkeypatch.setattr(workflow_voice_test, "capture_voice_provider_intent", capture)
    response = client.post(
        "/workflow/185/test-voice/provider-intent",
        json=intent_request(),
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 200 and response.json() == {
        "configuration_hash": "b" * 64
    }
    assert capture.await_args.kwargs == {
        **intent_request(),
        "workflow_id": 185,
        "organization_id": 42,
        "signing_secret": SECRET,
    }
    pool.assert_not_awaited()
    enqueue.assert_not_awaited()
    capture.reset_mock()
    client.app.dependency_overrides.clear()
    response = client.post(
        "/workflow/185/test-voice/provider-intent",
        json=intent_request(),
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code in {401, 403}
    capture.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"organization_id": 99},
        {"owner_user_id": 7},
        {"url": "https://client.invalid"},
        {"workflow_version": True},
        {"workflow_version": 0},
        {"workflow_version": "3"},
        {"runtime_workflow_hash": "bad"},
        {"version_kind": "active"},
    ],
)
def test_provider_intent_rejects_untrusted_scope_or_unpinned_requests(
    monkeypatch, extra
):
    client, _, _, _ = setup(monkeypatch)
    capture = AsyncMock()
    monkeypatch.setattr(workflow_voice_test, "capture_voice_provider_intent", capture)
    response = client.post(
        "/workflow/185/test-voice/provider-intent",
        json={**intent_request(), **extra},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 422
    capture.assert_not_awaited()


@pytest.mark.parametrize(
    "error,expected",
    [
        (RuntimeError("private-provider-key"), 503),
        (VoiceControlError("voice_workflow_not_found", 404), 404),
    ],
)
def test_provider_intent_http_errors_are_redacted(monkeypatch, error, expected):
    client, _, _, _ = setup(monkeypatch)
    monkeypatch.setattr(
        workflow_voice_test,
        "capture_voice_provider_intent",
        AsyncMock(side_effect=error),
    )
    response = client.post(
        "/workflow/185/test-voice/provider-intent",
        json=intent_request(),
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == expected
    assert "private-provider-key" not in response.text


def test_provider_intent_deadline_is_bounded_and_redacted(monkeypatch):
    client, _, _, _ = setup(monkeypatch)

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow_voice_test, "capture_voice_provider_intent", blocked)
    monkeypatch.setattr(workflow_voice_test, "VOICE_HTTP_DEADLINE_SECONDS", 0.001)
    response = client.post(
        "/workflow/185/test-voice/provider-intent",
        json=intent_request(),
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "voice_provider_intent_unavailable"}


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


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_voice_capabilities_require_internal_auth_before_queue_lookup(
    monkeypatch, secret
):
    client, _, pool, _ = setup(monkeypatch)
    response = client.get(
        "/workflow/test-voice/capabilities",
        headers={"x-sysevo-secret": secret} if secret else {},
    )
    assert response.status_code == 403
    pool.assert_not_awaited()


def test_voice_capabilities_expose_actual_worker_protocol_without_starting_a_job(
    monkeypatch,
):
    client, _, pool, enqueue = setup(monkeypatch)
    redis = pool.return_value
    redis.get.return_value = b"2"
    response = client.get(
        "/workflow/test-voice/capabilities", headers={"x-sysevo-secret": SECRET}
    )
    assert response.status_code == 200
    assert response.json()["worker_ready"] is True
    assert response.json()["independent_quality_evaluation"] is False
    enqueue.assert_not_awaited()
    redis.enqueue_job.assert_not_awaited()


def test_voice_capability_connection_failure_is_redacted(monkeypatch):
    client, _, pool, _ = setup(monkeypatch)
    pool.side_effect = RuntimeError("private queue credentials")
    response = client.get(
        "/workflow/test-voice/capabilities", headers={"x-sysevo-secret": SECRET}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "voice_worker_unavailable"}


def test_voice_capabilities_require_user_authentication(monkeypatch):
    client, _, pool, _ = setup(monkeypatch)
    client.app.dependency_overrides.clear()
    response = client.get(
        "/workflow/test-voice/capabilities", headers={"x-sysevo-secret": SECRET}
    )
    assert response.status_code in {401, 403}
    pool.assert_not_awaited()


@pytest.mark.parametrize("organization_id", [None, 0, True])
def test_voice_capabilities_require_valid_authenticated_scope(
    monkeypatch, organization_id
):
    client, user, pool, _ = setup(monkeypatch)
    user.selected_organization_id = organization_id
    response = client.get(
        "/workflow/test-voice/capabilities", headers={"x-sysevo-secret": SECRET}
    )
    assert response.status_code == 400
    pool.assert_not_awaited()


@pytest.mark.parametrize("operation", ["sessions", "status"])
@pytest.mark.parametrize("stalled_stage", ["pool", "operation"])
def test_private_voice_request_deadline_cancels_stalled_dependencies(
    monkeypatch, operation, stalled_stage
):
    client, _, pool, enqueue = setup(monkeypatch)
    read_status = AsyncMock(return_value={"status": "running"})
    monkeypatch.setattr(workflow_voice_test, "read_voice_job_status", read_status)
    monkeypatch.setattr(
        workflow_voice_test, "VOICE_HTTP_DEADLINE_SECONDS", 0.01, raising=False
    )
    cancelled = []
    continued = []

    async def stalled(*args):
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        continued.append(True)
        return {"status": "queued", "session_id": SESSION}

    target = (
        pool
        if stalled_stage == "pool"
        else enqueue
        if operation == "sessions"
        else read_status
    )
    target.side_effect = stalled
    response = client.post(
        f"/workflow/185/test-voice/{operation}",
        json={"session_id": SESSION, "lease_token": LEASE},
        headers={"x-sysevo-secret": SECRET},
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "voice_queue_unavailable"
        if operation == "sessions"
        else "voice_report_unavailable"
    }
    assert cancelled == [True] and continued == []
    assert LEASE not in response.text and SECRET not in response.text
    if stalled_stage == "pool":
        enqueue.assert_not_awaited()
        read_status.assert_not_awaited()
