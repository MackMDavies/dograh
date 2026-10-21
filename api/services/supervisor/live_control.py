"""Supervisor controls for a live AI-agent call: Whisper and Take over.

    portal --POST /workflow/runs/{id}/supervisor--> any api worker
           --redis supervisor:run:{id}--> the worker running that call's pipeline

WHY REDIS. The pipeline lives on whichever uvicorn worker accepted the provider's media
WebSocket; the supervisor's request lands on any worker (nginx least_conn). The worker that
owns the call subscribes to its run's channel for as long as the pipeline runs and keeps
a heartbeat key alive, so the route can answer "this call is not controllable" (409)
instead of publishing into the void.

WHISPER appends a private note to the LLM's context. The caller never hears it; the agent
follows it from its next turn. It is appended as a message rather than set as the system
instruction because every node transition replaces the system instruction.

TAKE OVER moves the caller to the supervisor's phone through the provider's existing
transfer path (the one the transfer_call tool uses): dial the supervisor, hold music
meanwhile, and on answer end the pipeline with EndTaskReason.TRANSFER_CALL so the
serializer's strategy bridges the caller into the conference. If the supervisor does not
answer, the AI simply carries on.

Nothing here may break a live call: every Redis or provider failure is logged and dropped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Optional

import redis.asyncio as aioredis
from loguru import logger

if TYPE_CHECKING:
    from api.services.workflow.pipecat_engine import PipecatEngine

_CHANNEL_PREFIX = "supervisor:run:"
_LIVE_PREFIX = "supervisor:live:"
# The heartbeat is refreshed every few seconds; a crashed worker's key lapses quickly.
_LIVE_TTL_SECONDS = 15
_HEARTBEAT_SECONDS = 5

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
MAX_WHISPER_CHARS = 600
TAKEOVER_TIMEOUT_SECONDS = 30
# The transfer context needs a tool id; a supervisor take-over has no tool.
TAKEOVER_TOOL_ID = "supervisor-takeover"


def channel_for(run_id: int | str) -> str:
    return f"{_CHANNEL_PREFIX}{run_id}"


def live_key_for(run_id: int | str) -> str:
    return f"{_LIVE_PREFIX}{run_id}"


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or "redis://redis:6379"


# Internal (server-published) command: the dialer's extra-answer chain hands a person
# the agent is talking to back to the rep who just joined their conference. It is never
# accepted from the portal (validate_command does not know it) and only ever moves this
# run's OWN call into that call's own room, call-<call SID>.
DIALER_TRANSFER = "dialer_transfer"
DIALER_TRANSFER_TOOL_ID = "dialer-extra-answer"


# Internal (server-published): store the agent's summary for the rep's card before the
# transfer-back offer is written. Only acted on by a dialer extra-answer run.
DIALER_SUMMARY = "dialer_summary"


def dialer_summary_command() -> dict[str, Any]:
    return {"type": DIALER_SUMMARY}


def dialer_transfer_command(*, conference_name: str, to_name: str, to_user_id: str) -> dict[str, Any]:
    return {
        "type": DIALER_TRANSFER, "conference_name": conference_name,
        "to_name": to_name, "to_user_id": to_user_id,
    }


def conversation_summary(messages: Any, *, limit: int = 900) -> str:
    """The last few spoken turns, for the rep's card when the agent hands over."""
    lines: list[str] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        text = " ".join(str(content or "").split())
        if not text or text.startswith("[Private instruction"):
            continue
        lines.append(("Them: " if message["role"] == "user" else "Agent: ") + text)
    summary = "\n".join(lines[-6:])
    return summary[-limit:]


class SupervisorCommandError(ValueError):
    """A command that can never be carried out (bad input), as opposed to a failure."""


def validate_command(body: dict[str, Any]) -> dict[str, Any]:
    """Normalise a portal request into the command published to the pipeline."""
    kind = body.get("type")
    if kind == "whisper":
        text = str(body.get("text") or "").strip()
        if not text:
            raise SupervisorCommandError("Write the instruction for the agent first")
        if len(text) > MAX_WHISPER_CHARS:
            raise SupervisorCommandError(
                f"Keep a whisper under {MAX_WHISPER_CHARS} characters"
            )
        return {"type": "whisper", "text": text}
    if kind == "takeover":
        destination = re.sub(r"[\s\-().]", "", str(body.get("destination") or ""))
        if not E164.match(destination):
            raise SupervisorCommandError(
                "Enter the number to ring in international format, e.g. +14155550123"
            )
        return {"type": "takeover", "destination": destination}
    raise SupervisorCommandError("Unknown supervisor command")


def whisper_message(text: str) -> dict[str, str]:
    """The context message a whisper becomes. Framed so the model neither reads it aloud
    nor mentions that it was coached."""
    return {
        "role": "user",
        "content": (
            "[Private instruction from your supervisor. The caller cannot hear this. "
            "Do not read it out or mention it; follow it from your next reply.]\n"
            + text
        ),
    }


async def is_controllable(run_id: int | str) -> bool:
    client = aioredis.from_url(_redis_url())
    try:
        return bool(await client.exists(live_key_for(run_id)))
    finally:
        with suppress(Exception):
            await client.aclose()


async def publish_command(run_id: int | str, command: dict[str, Any]) -> int:
    """Returns how many pipelines received it (0 = nobody is running this call)."""
    client = aioredis.from_url(_redis_url())
    try:
        return int(await client.publish(channel_for(run_id), json.dumps(command)))
    finally:
        with suppress(Exception):
            await client.aclose()


class SupervisorControl:
    """Runs beside one call's pipeline on the worker that owns it."""

    def __init__(
        self,
        engine: "PipecatEngine",
        workflow_run_id: int,
        organization_id: Optional[int],
    ):
        self._engine = engine
        self._run_id = workflow_run_id
        self._org_id = organization_id
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._takeover_running = False

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._listen(), name=f"supervisor-{self._run_id}"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(BaseException):
                await self._task
        client = aioredis.from_url(_redis_url())
        with suppress(Exception):
            await client.delete(live_key_for(self._run_id))
        with suppress(Exception):
            await client.aclose()

    async def _listen(self) -> None:
        client = aioredis.from_url(_redis_url())
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(channel_for(self._run_id))
            next_beat = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_beat:
                    with suppress(Exception):
                        await client.set(
                            live_key_for(self._run_id), "1", ex=_LIVE_TTL_SECONDS
                        )
                    next_beat = now + _HEARTBEAT_SECONDS
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    continue
                if not message:
                    continue
                try:
                    command = json.loads(message.get("data") or b"{}")
                except (TypeError, ValueError):
                    continue
                # Handled off the listen loop so a take-over's 30s wait doesn't stop the heartbeat.
                asyncio.create_task(self._handle(command))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 -- never break the call
            logger.warning(f"supervisor listener for run {self._run_id} ended: {exc}")
        finally:
            with suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()
            with suppress(Exception):
                await client.aclose()

    async def _handle(self, command: dict[str, Any]) -> None:
        if isinstance(command, dict) and command.get("type") == DIALER_SUMMARY:
            try:
                await self.dialer_summary()
            except Exception as exc:  # noqa: BLE001
                logger.info(f"dialer summary failed for run {self._run_id}: {exc}")
            return
        if isinstance(command, dict) and command.get("type") == DIALER_TRANSFER:
            try:
                await self.dialer_transfer(command)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"dialer transfer-back failed for run {self._run_id}: {exc}")
            return
        try:
            command = validate_command(command)
        except SupervisorCommandError:
            return
        try:
            if command["type"] == "whisper":
                await self.whisper(command["text"])
            else:
                await self.take_over(command["destination"])
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"supervisor {command['type']} failed for run {self._run_id}: {exc}"
            )

    async def whisper(self, text: str) -> None:
        from pipecat.frames.frames import LLMMessagesAppendFrame

        task = self._engine.task
        if not task:
            return
        await task.queue_frame(
            LLMMessagesAppendFrame([whisper_message(text)], run_llm=False)
        )
        logger.info(f"supervisor whisper delivered to run {self._run_id}")

    def _messages(self) -> Any:
        engine = self._engine
        if getattr(engine, "context", None) is not None:
            with suppress(Exception):
                return engine.context.get_messages()
        return []

    async def _dialer_call_id(self) -> str | None:
        from api.db import db_client

        run = await db_client.get_workflow_run_by_id(self._run_id)
        context = (run.initial_context or {}) if run else {}
        call_id = str(((run.gathered_context or {}) if run else {}).get("call_id") or "")
        if not run or not context.get("dialer_extra_answer_id") or not call_id \
                or context.get("dialer_original_call_id") != call_id:
            return None
        return call_id

    async def dialer_summary(self) -> None:
        """Store a short summary of this extra-answer call (key facts, or an LLM's)."""
        from api.services.telephony.dialer import extra_answer

        call_id = await self._dialer_call_id()
        if not call_id:
            return
        summary = await extra_answer.agent_summary(self._messages())
        if summary:
            await extra_answer.set_summary(call_id, summary)

    async def dialer_transfer(self, command: dict[str, Any]) -> None:
        """Hand this agent call to the rep who joined call-<call SID> (extra answer)."""
        from pipecat.frames.frames import TTSSpeakFrame
        from pipecat.utils.enums import EndTaskReason

        from api.db import db_client
        from api.services.telephony.call_transfer_manager import get_call_transfer_manager
        from api.services.telephony.dialer import extra_answer
        from api.services.telephony.transfer_event_protocol import TransferContext

        if self._takeover_running:
            return
        run = await db_client.get_workflow_run_by_id(self._run_id)
        context = (run.initial_context or {}) if run else {}
        call_id = str(((run.gathered_context or {}) if run else {}).get("call_id") or "")
        conference_name = str(command.get("conference_name") or "")
        if (
            not run or not context.get("dialer_extra_answer_id") or not call_id
            or context.get("dialer_original_call_id") != call_id
            or conference_name != f"call-{call_id}"
        ):
            logger.warning(f"dialer transfer-back refused for run {self._run_id}")
            return
        self._takeover_running = True
        engine = self._engine
        try:
            summary = await extra_answer.agent_summary(self._messages())
            if summary:
                await extra_answer.set_summary(call_id, summary)
            manager = await get_call_transfer_manager()
            await manager.store_transfer_context(TransferContext(
                transfer_id=str(uuid.uuid4()), call_sid=None, target_number="",
                tool_uuid=DIALER_TRANSFER_TOOL_ID, original_call_sid=call_id,
                conference_name=conference_name, initiated_at=time.time(),
                workflow_run_id=self._run_id,
            ))
            task = engine.task
            if task:
                await task.queue_frame(TTSSpeakFrame(
                    extra_answer.transfer_line(str(command.get("to_name") or "")), append_to_context=False,
                ))
            # The serializer's transfer strategy moves the caller into the conference once
            # the line above has been spoken; the rep is already in it. The transfer is only
            # recorded when the person's leg joins that room (dialer_extra_answer
            # .on_prospect_joined); if it never does, the worker recovers it.
            await engine.end_call_with_reason(EndTaskReason.TRANSFER_CALL.value, abort_immediately=False)
        except Exception:
            # Nothing was handed over: let the worker try again rather than leave the rep
            # waiting in the room.
            self._takeover_running = False
            await extra_answer.release_transfer(call_id)
            raise
        logger.info(f"dialer extra answer handover started from run {self._run_id}")

    async def take_over(self, destination: str) -> None:
        if self._takeover_running:
            return
        self._takeover_running = True
        try:
            await self._take_over(destination)
        finally:
            self._takeover_running = False

    async def _take_over(self, destination: str) -> None:
        from pipecat.utils.enums import EndTaskReason

        from api.db import db_client
        from api.enums import WorkflowRunMode
        from api.services.pipecat.audio_playback import play_audio_loop
        from api.services.telephony.call_transfer_manager import (
            get_call_transfer_manager,
        )
        from api.services.telephony.factory import get_telephony_provider_for_run
        from api.services.telephony.transfer_event_protocol import TransferContext

        run = await db_client.get_workflow_run_by_id(self._run_id)
        if not run or not self._org_id:
            return
        if run.mode in (
            WorkflowRunMode.WEBRTC.value,
            WorkflowRunMode.SMALLWEBRTC.value,
            WorkflowRunMode.TEXTCHAT.value,
        ):
            logger.info(
                f"supervisor take-over skipped for run {self._run_id}: not a phone call"
            )
            return
        provider = await get_telephony_provider_for_run(run, self._org_id)
        if not provider.supports_transfers() or not provider.validate_config():
            logger.info(
                f"supervisor take-over skipped for run {self._run_id}: provider cannot transfer"
            )
            return
        original_call_sid = (run.gathered_context or {}).get("call_id")
        if not original_call_sid:
            logger.info(
                f"supervisor take-over skipped for run {self._run_id}: no provider call id"
            )
            return

        manager = await get_call_transfer_manager()
        transfer_id = str(uuid.uuid4())
        context = TransferContext(
            transfer_id=transfer_id,
            call_sid=None,
            target_number=destination,
            tool_uuid=TAKEOVER_TOOL_ID,
            original_call_sid=original_call_sid,
            conference_name=f"transfer-{original_call_sid}",
            initiated_at=time.time(),
            workflow_run_id=self._run_id,
        )
        await manager.store_transfer_context(context)

        engine = self._engine
        engine.set_mute_pipeline(True)
        stop_music = asyncio.Event()
        music: Optional[asyncio.Task] = None
        event = None
        try:
            result = await provider.transfer_call(
                destination=destination,
                transfer_id=transfer_id,
                conference_name=context.conference_name,
                timeout=TAKEOVER_TIMEOUT_SECONDS,
            )
            context.call_sid = result.get("call_sid")
            await manager.store_transfer_context(context)
            if engine._transport_output is not None:
                rate = (
                    engine._audio_config.transport_out_sample_rate
                    if engine._audio_config
                    else 8000
                )
                music = asyncio.create_task(
                    play_audio_loop(
                        stop_event=stop_music,
                        sample_rate=rate,
                        queue_frame=engine._transport_output.queue_frame,
                    )
                )
            event = await manager.wait_for_transfer_completion(
                transfer_id, TAKEOVER_TIMEOUT_SECONDS
            )
        finally:
            stop_music.set()
            if music:
                with suppress(Exception):
                    await music
            engine.set_mute_pipeline(False)

        answered = (
            bool(event)
            and event.to_result_dict().get("action") == "destination_answered"
        )
        if answered:
            logger.info(f"supervisor took over run {self._run_id}")
            await engine.end_call_with_reason(
                EndTaskReason.TRANSFER_CALL.value, abort_immediately=False
            )
        else:
            # The supervisor missed it; the AI keeps the call. Drop the stale context so the
            # teardown strategy can't bridge into a conference nobody is in.
            with suppress(Exception):
                await manager.remove_transfer_context(transfer_id)
            logger.info(
                f"supervisor take-over not answered for run {self._run_id}; AI keeps the call"
            )
