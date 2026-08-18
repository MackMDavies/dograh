"""dial_permission_check fails CLOSED when it cannot reach a verdict.

The permissive answer here is byte-identical to a genuine "this number is
fine", which is exactly why this needs a positive test: if the gate silently
starts allowing everything again, nothing else in the system notices. Quiet
logs are not evidence.

Deliberately importable without pipecat — the module under test pulls in only
httpx and loguru, so these run anywhere.
"""

import asyncio
import os

import httpx
import pytest

from api.services import dial_permission_check as m

NUMBER = "+15550000000"
WORKFLOW_ID = 1


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient, replaying one scripted behaviour."""

    def __init__(self, behaviour: str):
        self._behaviour = behaviour

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, *_args, **_kwargs):
        request = httpx.Request("POST", "https://example.invalid/check")
        if self._behaviour == "timeout":
            raise httpx.TimeoutException("simulated timeout")
        if self._behaviour == "error":
            raise RuntimeError("simulated unhandled error")
        if self._behaviour == "http500":
            return httpx.Response(500, json={}, request=request)
        if self._behaviour == "blocked":
            return httpx.Response(
                200,
                json={
                    "call_inbound": {
                        "dynamic_variables": {
                            "dial_blocked": "true",
                            "dial_block_reason": "suppressed",
                        }
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"call_inbound": {"dynamic_variables": {"dial_blocked": "false"}}},
            request=request,
        )


@pytest.fixture
def call_gate(monkeypatch):
    """Returns a callable running check_dial_permitted against a scripted client."""
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://example.invalid/check")
    monkeypatch.delenv("SYSEVO_DIAL_CHECK_FAIL_OPEN", raising=False)

    def _run(behaviour: str):
        monkeypatch.setattr(
            m.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(behaviour)
        )
        return asyncio.run(m.check_dial_permitted(WORKFLOW_ID, NUMBER))

    return _run


@pytest.mark.parametrize("behaviour", ["timeout", "error", "http500"])
def test_unreachable_gate_defers_instead_of_dialling(call_gate, behaviour):
    """A gate that cannot answer must not hand out permission to dial."""
    allowed, reason, retry_at = call_gate(behaviour)
    assert allowed is False
    assert reason == m.REASON_CHECK_UNAVAILABLE
    # retry_at is what stops the dispatcher marking the row failed and
    # destroying the lead — it must always be present on this path.
    assert retry_at is not None


def test_permitted_number_still_dials(call_gate):
    assert call_gate("ok") == (True, "", None)


def test_blocked_number_still_blocks(call_gate):
    allowed, reason, retry_at = call_gate("blocked")
    assert (allowed, reason, retry_at) == (False, "suppressed", None)


def test_kill_switch_restores_fail_open(call_gate, monkeypatch):
    """Opting back into dialling through an outage stays possible, but explicit."""
    monkeypatch.setenv("SYSEVO_DIAL_CHECK_FAIL_OPEN", "true")
    assert call_gate("timeout") == (True, "", None)


def test_unconfigured_deployment_is_unaffected(monkeypatch):
    """The OSS distribution runs without this enforcement wired up at all."""
    monkeypatch.delenv("SYSEVO_PRE_CALL_CHECK_URL", raising=False)
    assert asyncio.run(m.check_dial_permitted(WORKFLOW_ID, NUMBER)) == (True, "", None)
