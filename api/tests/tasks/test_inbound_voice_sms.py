from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.tasks import inbound_voice_sms as task


def _redis():
    return SimpleNamespace(set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1))


@pytest.mark.asyncio
async def test_inbound_voice_sms_runs_published_text_turn_and_sends_once(monkeypatch):
    redis = _redis()
    run = SimpleNamespace(id=321)
    session = SimpleNamespace(revision=1, session_data={"turns": []})
    workflow = SimpleNamespace(user_id=44)
    get_workflow = AsyncMock(return_value=workflow)
    create_run = AsyncMock(return_value=run)
    append = AsyncMock(return_value=session)
    execute = AsyncMock()
    send = AsyncMock(return_value={"status": "sent"})

    async def execute_turn(**_kwargs):
        session.session_data = {"turns": [{
            "source_event_id": "SM" + "a" * 32,
            "status": "completed",
            "assistant_message": {"text": "Your appointment is confirmed."},
        }]}
        return session

    execute.side_effect = execute_turn
    monkeypatch.setattr(task.db_client, "get_workflow", get_workflow)
    monkeypatch.setattr(task.db_client, "create_workflow_run", create_run)
    monkeypatch.setattr(task.db_client, "ensure_workflow_run_text_session", AsyncMock(return_value=session))
    monkeypatch.setattr(task, "check_dograh_quota_by_user_id", AsyncMock(return_value=SimpleNamespace(has_quota=True)))
    monkeypatch.setattr(task, "append_text_chat_user_message", append)
    monkeypatch.setattr(task, "execute_pending_text_chat_turn", execute)
    monkeypatch.setattr(task, "send_inbound_sms_reply", send)

    await task.process_inbound_voice_sms(
        {"redis": redis}, 99, 7, 88, "+15551230002", "+15551230001",
        "Can I book a demo?", "SM" + "a" * 32,
    )

    get_workflow.assert_awaited_once_with(99, organization_id=7)
    create_run.assert_awaited_once()
    append.assert_awaited_once()
    execute.assert_awaited_once()
    send.assert_awaited_once_with(
        workflow_id=99, workflow_run_id=321, to="+15551230001",
        body="Your appointment is confirmed.", message_sid="SM" + "a" * 32,
    )
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_voice_sms_reuses_completed_turn_on_retry(monkeypatch):
    redis = _redis()
    message_sid = "SM" + "b" * 32
    session = SimpleNamespace(revision=2, session_data={"turns": [{
        "source_event_id": message_sid,
        "status": "completed",
        "assistant_message": {"text": "The office opens at nine."},
    }]})
    monkeypatch.setattr(task.db_client, "get_workflow", AsyncMock(return_value=SimpleNamespace(user_id=44)))
    monkeypatch.setattr(task.db_client, "create_workflow_run", AsyncMock(return_value=SimpleNamespace(id=654)))
    monkeypatch.setattr(task.db_client, "ensure_workflow_run_text_session", AsyncMock(return_value=session))
    monkeypatch.setattr(task, "check_dograh_quota_by_user_id", AsyncMock(return_value=SimpleNamespace(has_quota=True)))
    monkeypatch.setattr(task, "append_text_chat_user_message", AsyncMock(return_value=session))
    execute = AsyncMock()
    send = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(task, "execute_pending_text_chat_turn", execute)
    monkeypatch.setattr(task, "send_inbound_sms_reply", send)

    await task.process_inbound_voice_sms(
        {"redis": redis}, 99, 7, 88, "+15551230002", "+15551230001",
        "Where is the office?", message_sid,
    )

    execute.assert_not_awaited()
    send.assert_awaited_once_with(
        workflow_id=99, workflow_run_id=654, to="+15551230001",
        body="The office opens at nine.", message_sid=message_sid,
    )


@pytest.mark.asyncio
async def test_inbound_voice_sms_stops_before_creating_run_when_quota_is_exhausted(monkeypatch):
    create_run = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(task.db_client, "get_workflow", AsyncMock(return_value=SimpleNamespace(user_id=44)))
    monkeypatch.setattr(task.db_client, "create_workflow_run", create_run)
    monkeypatch.setattr(task, "check_dograh_quota_by_user_id", AsyncMock(return_value=SimpleNamespace(has_quota=False)))
    monkeypatch.setattr(task, "send_inbound_sms_reply", send)

    await task.process_inbound_voice_sms(
        {"redis": _redis()}, 99, 7, 88, "+15551230002", "+15551230001",
        "Can I book a demo?", "SM" + "c" * 32,
    )

    create_run.assert_not_awaited()
    send.assert_not_awaited()
