"""Run one inbound SMS through the owning voice workflow and send its reply."""

from __future__ import annotations

import hashlib

from loguru import logger
from pipecat.utils.run_context import set_current_org_id, set_current_run_id

from api.db import db_client
from api.enums import WorkflowRunMode
from api.services.phone_tools.builtin import send_inbound_sms_reply
from api.services.quota_service import check_dograh_quota_by_user_id
from api.services.workflow.text_chat_session_service import (
    TextChatSessionExecutionError,
    TextChatSessionRevisionConflictError,
    append_text_chat_user_message,
    default_text_chat_checkpoint,
    default_text_chat_session_data,
    execute_pending_text_chat_turn,
)


def _conversation_key(organization_id: int, phone_number_id: int, caller: str) -> str:
    digest = hashlib.sha256(
        f"voice-sms:{organization_id}:{phone_number_id}:{caller}".encode()
    ).hexdigest()
    return f"voice-sms:{digest}"


async def process_inbound_voice_sms(
    ctx,
    workflow_id: int,
    organization_id: int,
    phone_number_id: int,
    to_number: str,
    from_number: str,
    body: str,
    message_sid: str,
) -> None:
    """Execute a published text-chat turn, scoped to the matched number's org.

    The queue job ID is the provider SID. The text-session event ID and the SMS
    send key use that same SID, so retries do not duplicate either side effect.
    """
    redis = ctx["redis"]
    conversation = _conversation_key(organization_id, phone_number_id, from_number)
    lock_key = f"{conversation}:lock"
    lock_token = message_sid
    acquired = await redis.set(lock_key, lock_token, ex=180, nx=True)
    if not acquired:
        raise RuntimeError("Inbound SMS conversation is already processing")

    try:
        workflow = await db_client.get_workflow(
            workflow_id, organization_id=organization_id
        )
        if not workflow or not workflow.user_id:
            logger.warning("Inbound voice SMS skipped: workflow owner unavailable")
            return

        quota = await check_dograh_quota_by_user_id(
            workflow.user_id, workflow_id=workflow_id
        )
        if not quota.has_quota:
            logger.info("Inbound voice SMS reply skipped: agent quota unavailable")
            return

        run = await db_client.create_workflow_run(
            name=f"WR-SMS-{message_sid[-8:]}",
            workflow_id=workflow_id,
            mode=WorkflowRunMode.TEXTCHAT.value,
            user_id=workflow.user_id,
            initial_context={
                "provider": "twilio",
                "source": "voice_number_sms",
                "from_number": from_number,
                "to_number": to_number,
            },
            use_draft=False,
            organization_id=organization_id,
            idempotency_key=conversation,
        )
        set_current_org_id(organization_id)
        set_current_run_id(run.id)

        text_session = await db_client.ensure_workflow_run_text_session(
            run.id,
            session_data=default_text_chat_session_data(),
            checkpoint=default_text_chat_checkpoint(),
        )
        try:
            text_session = await append_text_chat_user_message(
                run_id=run.id,
                text_session=text_session,
                user_text=body,
                expected_revision=text_session.revision,
                source_event_id=message_sid,
            )
        except TextChatSessionRevisionConflictError:
            # A retried ARQ job reloads the current revision before it appends.
            raise

        session_data = text_session.session_data or {}
        matching_turn = next(
            (
                turn for turn in reversed(session_data.get("turns") or [])
                if turn.get("source_event_id") == message_sid
            ),
            None,
        )
        if matching_turn and matching_turn.get("status") == "completed":
            answer = (matching_turn.get("assistant_message") or {}).get("text")
        else:
            try:
                text_session = await execute_pending_text_chat_turn(
                    workflow_id=workflow_id,
                    run_id=run.id,
                    text_session=text_session,
                )
            except TextChatSessionExecutionError:
                logger.warning("Inbound voice SMS turn failed for workflow %s", workflow_id)
                raise
            turns = (text_session.session_data or {}).get("turns") or []
            matching_turn = next(
                (
                    turn for turn in reversed(turns)
                    if turn.get("source_event_id") == message_sid
                ),
                None,
            )
            answer = (matching_turn or {}).get("assistant_message", {}).get("text")

        if not isinstance(answer, str) or not answer.strip():
            logger.info("Inbound voice SMS turn produced no text for workflow %s", workflow_id)
            return

        result = await send_inbound_sms_reply(
            workflow_id=workflow_id,
            workflow_run_id=run.id,
            to=from_number,
            body=answer,
            message_sid=message_sid,
        )
        if result.get("status") not in ("sent", "queued"):
            logger.warning(
                "Inbound voice SMS reply not sent for workflow %s: %s",
                workflow_id,
                result.get("reason") or "unknown",
            )
    finally:
        await redis.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
            1,
            lock_key,
            lock_token,
        )
