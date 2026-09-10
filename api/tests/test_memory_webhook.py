"""fire_post_call_memory must never treat a missing `caller_number` key as a
reason to drop the whole post-call notification, and must pick the LEAD's
number rather than our own outbound caller ID on an outbound run.

The bug this guards against: `initial_context` on this deployment frequently
has no `caller_number` key at all (campaign dispatch populates `called_number`
instead), and when it *does* have `caller_number` on an outbound run, that
value is our own SIP caller ID (+16592127650, identical on every run) — not
the lead. The old code read only `initial_context["caller_number"]` and
returned early (terminal, no retry) whenever it was empty, which silently
dropped every post-call notification for those runs: no analysis, no caller
memory, nothing in the Inbound inbox, and no error anywhere because a skip
reports success. See project memory `project_caller_identity_pipeline`.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from api.services import memory_webhook  # noqa: E402


def _make_run(
    *,
    call_type="outbound",
    initial_context=None,
    logs=None,
    gathered_context=None,
    usage_info=None,
    workflow_id=42,
):
    run = MagicMock()
    run.workflow_id = workflow_id
    run.call_type = call_type
    run.initial_context = initial_context or {}
    run.logs = logs or {"realtime_feedback_events": []}
    run.gathered_context = gathered_context or {}
    run.usage_info = usage_info or {}
    return run


def _mock_post_client(status_code=200, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.text = ""
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture(autouse=True)
def _memory_url_configured(monkeypatch):
    monkeypatch.setenv("SYSEVO_POST_CALL_MEMORY_URL", "https://example.test/memory")
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "shh")


@pytest.mark.asyncio
async def test_outbound_run_with_no_caller_number_key_still_posts():
    """The exact production shape: initial_context carries `called_number`
    (what the campaign dispatcher writes) but no `caller_number` key at all.
    The old code's `if not caller_number: return True` dropped this silently."""
    run = _make_run(
        call_type="outbound",
        initial_context={"called_number": "+442071234567", "first_name": "Alex"},
    )
    mock_client = _mock_post_client()

    with patch.object(memory_webhook.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch("api.services.memory_webhook.httpx.AsyncClient", return_value=mock_client):
        settled = await memory_webhook.fire_post_call_memory(1)

    assert settled is True
    assert mock_client.post.await_count == 1
    _, kwargs = mock_client.post.await_args
    assert kwargs["json"]["caller_number"] == "+442071234567"


@pytest.mark.asyncio
async def test_outbound_run_never_reports_our_own_caller_id_as_the_lead():
    """initial_context carries BOTH our own outbound caller ID under
    `caller_number` and the real lead under `called_number` — the shape that
    currently produces 265 call_analysis rows keyed on our own number."""
    run = _make_run(
        call_type="outbound",
        initial_context={
            "caller_number": "+16592127650",  # our own outbound ID
            "called_number": "+15095551234",  # the lead we dialled
        },
    )
    mock_client = _mock_post_client()

    with patch.object(memory_webhook.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch("api.services.memory_webhook.httpx.AsyncClient", return_value=mock_client):
        await memory_webhook.fire_post_call_memory(1)

    _, kwargs = mock_client.post.await_args
    assert kwargs["json"]["caller_number"] == "+15095551234"
    assert kwargs["json"]["caller_number"] != "+16592127650"


@pytest.mark.asyncio
async def test_inbound_run_prefers_caller_number_over_called_number():
    """On inbound the direction flips: the party who rang US is the lead."""
    run = _make_run(
        call_type="inbound",
        initial_context={
            "caller_number": "+15095551234",  # the person who rang in
            "called_number": "+16592127650",  # our own inbound DID
        },
    )
    mock_client = _mock_post_client()

    with patch.object(memory_webhook.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch("api.services.memory_webhook.httpx.AsyncClient", return_value=mock_client):
        await memory_webhook.fire_post_call_memory(1)

    _, kwargs = mock_client.post.await_args
    assert kwargs["json"]["caller_number"] == "+15095551234"


@pytest.mark.asyncio
async def test_payload_forwards_initial_context_and_call_type():
    """dograh-post-call-memory's own direction-aware resolution
    (leadNumberForCall) needs these two fields to do anything at all — sending
    a pre-resolved caller_number without them means the Sysevo side can never
    apply its own fallback order if this side's answer is wrong."""
    ctx = {"called_number": "+442071234567", "first_name": "Alex", "company": "Acme"}
    run = _make_run(call_type="outbound", initial_context=ctx)
    mock_client = _mock_post_client()

    with patch.object(memory_webhook.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch("api.services.memory_webhook.httpx.AsyncClient", return_value=mock_client):
        await memory_webhook.fire_post_call_memory(1)

    _, kwargs = mock_client.post.await_args
    assert kwargs["json"]["initial_context"] == ctx
    assert kwargs["json"]["call_type"] == "outbound"


@pytest.mark.asyncio
async def test_run_with_truly_no_number_anywhere_still_posts_for_the_transcript():
    """Even with zero phone numbers anywhere, the transcript is still worth
    sending — the Sysevo endpoint keys on web:run:<id> when there is no
    number. Dropping the call here loses analysis and the Inbound inbox
    entry, not just caller memory."""
    run = _make_run(call_type="outbound", initial_context={})
    mock_client = _mock_post_client()

    with patch.object(memory_webhook.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch("api.services.memory_webhook.httpx.AsyncClient", return_value=mock_client):
        settled = await memory_webhook.fire_post_call_memory(1)

    assert settled is True
    assert mock_client.post.await_count == 1
    _, kwargs = mock_client.post.await_args
    assert kwargs["json"]["caller_number"] is None
