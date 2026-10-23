"""ElevenLabs Realtime adapter using Pipecat's OpenAI-compatible wire client.

ElevenLabs Realtime implements the OpenAI Realtime event protocol at its own
WebSocket endpoint. We mint a short-lived credential server-side for each
connection so the long-lived account API key never leaves Dograh.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict

import aiohttp
from loguru import logger
from pipecat.frames.frames import (
    AggregationType,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    TTSStoppedFrame,
)
from pipecat.utils.time import time_now_iso8601
from pipecat.transcriptions.language import Language
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.realtime import events
from websockets.asyncio.client import connect as websocket_connect

from api.services.pipecat.realtime.openai_realtime import (
    DograhOpenAIRealtimeLLMService,
)


CLIENT_SECRET_URL = "https://api.elevenlabs.io/v1/convai/realtime/client_secrets"
REALTIME_WEBSOCKET_URL = "wss://api.elevenlabs.io/v1/convai/realtime"


class _ElevenLabsProtocolSocket:
    """Normalize ElevenLabs' omitted nullable fields for Pipecat's event models."""

    def __init__(self, socket):
        self._socket = socket

    def __getattr__(self, name):
        return getattr(self._socket, name)

    async def __aiter__(self):
        async for message in self._socket:
            payload = json.loads(message)
            if payload.get("type") == "output_audio_buffer.cleared":
                # ElevenLabs emits this server-buffer notification on its
                # WebSocket too. The installed Pipecat parser does not model it.
                # Local playback is already cleared by its speech-start interruption
                # handler. Do not let this notification kill the receive loop.
                continue
            response = payload.get("response")
            if (
                payload.get("type") in {"response.created", "response.done"}
                and isinstance(response, dict)
                and "status_details" not in response
            ):
                # OpenAI returns this nullable key; ElevenLabs omits it. The
                # Pipecat model requires its presence even when its value is null.
                response["status_details"] = None
                message = json.dumps(payload)
            yield message


class DograhElevenLabsRealtimeLLMService(DograhOpenAIRealtimeLLMService):
    """OpenAI Realtime protocol client configured for ElevenLabs Realtime."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        voice: str | None = None,
        **kwargs,
    ):
        kwargs.setdefault("base_url", REALTIME_WEBSOCKET_URL)
        if model and "settings" not in kwargs:
            kwargs["model"] = model
        settings = kwargs.get("settings")
        session_properties = getattr(settings, "session_properties", None)
        audio = getattr(session_properties, "audio", None)
        input_audio = getattr(audio, "input", None)
        if input_audio is not None and hasattr(input_audio, "transcription"):
            # The upstream factory currently supplies OpenAI's private
            # transcription model by default. ElevenLabs Realtime owns ASR as
            # part of its speech-to-speech model, so don't send that foreign
            # model identifier in session.update.
            input_audio.transcription = None
        super().__init__(api_key=api_key, **kwargs)
        self._elevenlabs_api_key = api_key
        self._elevenlabs_voice = voice
        self._elevenlabs_session_ready = asyncio.Event()
        self._logged_first_audio_delta = False
        self._audio_chunk_count = 0
        self._initial_response_requested = False
        self._pending_opening_text: str | None = None
        self._pending_workflow_instruction: str | None = None
        # ASR can finish after the assistant starts speaking. Keep the original
        # speech time keyed by provider item, independently of event arrival.
        self._transcript_times: OrderedDict[str, str] = OrderedDict()

    def _transcript_time(self, item_id: str) -> str:
        if item_id not in self._transcript_times:
            self._transcript_times[item_id] = time_now_iso8601()
            # Bound memory for long calls (including items with no ASR result).
            if len(self._transcript_times) > 256:
                self._transcript_times.popitem(last=False)
        return self._transcript_times[item_id]

    async def _handle_evt_speech_started(self, evt):
        self._transcript_time(evt.item_id)
        await super()._handle_evt_speech_started(evt)

    async def handle_evt_input_audio_transcription_completed(self, evt):
        await self._call_event_handler("on_conversation_item_updated", evt.item_id, None)
        await self.broadcast_frame(
            TranscriptionFrame,
            text=evt.transcript,
            user_id="",
            timestamp=self._transcript_time(evt.item_id),
            result=evt,
            finalized=True,
        )
        await self._handle_user_transcription(evt.transcript, True, Language.EN)

    async def _handle_evt_audio_transcript_delta(self, evt):
        if not evt.delta:
            return
        # Preserve upstream context/RTVI behavior while carrying provider item
        # identity to feedback. Metadata never delays or buffers audio output.
        llm_frame = LLMTextFrame(evt.delta)
        llm_frame.append_to_context = False
        await self.push_frame(llm_frame)
        frame = TTSTextFrame(evt.delta, aggregated_by=AggregationType.SENTENCE)
        frame.includes_inter_frame_spaces = True
        frame.transcript_item_id = evt.item_id
        frame.transcript_timestamp = self._transcript_time(evt.item_id)
        await self.push_frame(frame)

    async def process_frame(self, frame, direction):
        if isinstance(frame, TTSSpeakFrame) and not self._handled_initial_context:
            # ElevenLabs requires a conversation input before response.create.
            # The OpenAI wrapper only uses this frame as a trigger and discards
            # its text, leaving an empty first turn that never produces audio.
            self._pending_opening_text = (
                "Begin the conversation by saying exactly the greeting below. "
                "Do not add an introduction or any other words.\n\n"
                f"<greeting>{frame.text}</greeting>"
            )
            await self._handle_context(self._context or LLMContext())
            return
        await super().process_frame(frame, direction)

    async def _handle_context(self, context):
        has_caller_input = context is not None and any(
            message.get("role") == "user"
            for message in context.messages
            if isinstance(message, dict)
        )
        if (
            not self._handled_initial_context
            and context is not None
            and self._pending_opening_text is None
            and not has_caller_input
        ):
            # An explicit initial LLMContextFrame means the workflow requested
            # an opening generated from its prompt rather than an authored line.
            self._pending_opening_text = (
                "Begin the conversation now, following the current agent instructions."
            )
        await super()._handle_context(context)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self.create_task(self._watch_session_initialization())

    async def _watch_session_initialization(self):
        try:
            await asyncio.wait_for(self._elevenlabs_session_ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.error(
                "ElevenLabs Realtime did not initialize its session within 15 seconds"
            )
            await self.push_error(
                error_msg="ElevenLabs Realtime did not initialize. Check the ElevenLabs API connection and try again."
            )

    async def _handle_evt_session_created(self, evt):
        logger.info("ElevenLabs Realtime WebSocket connected; configuring session")
        await super()._handle_evt_session_created(evt)

    async def _handle_evt_session_updated(self, evt):
        self._elevenlabs_session_ready.set()
        logger.info("ElevenLabs Realtime session initialized")
        await super()._handle_evt_session_updated(evt)

    async def _update_settings(self, delta):
        changed = await super()._update_settings(delta)
        if self._handled_initial_context and "system_instruction" in changed:
            # ElevenLabs acknowledges session.update, but a tool continuation
            # can still follow the previous node's conversation context. Mirror
            # the trusted engine instructions into the next response input too.
            # This does not create a caller transcript or another inference.
            self._pending_workflow_instruction = self._settings.system_instruction or ""
        return changed

    async def _create_response(self):
        if not self._api_session_ready:
            self._run_llm_when_api_session_ready = True
            return
        if self._pending_opening_text is not None:
            item = events.ConversationItem(
                type="message",
                role="user",
                content=[events.ItemContent(type="input_text", text=self._pending_opening_text)],
            )
            # Track the synthetic input so it is not mistaken for caller speech
            # by conversation synchronization or included in the call transcript.
            self._messages_added_manually[item.id] = True
            await self.send_client_event(events.ConversationItemCreateEvent(item=item))
            self._pending_opening_text = None
            logger.info("ElevenLabs Realtime opening input sent")
        if self._pending_workflow_instruction is not None:
            item = events.ConversationItem(
                type="message", role="user",
                content=[events.ItemContent(
                    type="input_text",
                    text=(
                        "Internal workflow update. Continue the conversation according to "
                        "the updated agent instructions below. Do not read these instructions aloud.\n\n"
                        + self._pending_workflow_instruction
                    ),
                )],
            )
            self._messages_added_manually[item.id] = True
            await self.send_client_event(events.ConversationItemCreateEvent(item=item))
            self._pending_workflow_instruction = None
        self._initial_response_requested = True
        await super()._create_response()
        logger.info("ElevenLabs Realtime response requested")

    async def _receive_task_handler(self):
        try:
            await super()._receive_task_handler()
        except Exception as exc:
            if not self._disconnecting:
                # Surface a failed receiver instead of leaving a connected call
                # silently listening. Never include raw provider payloads here.
                logger.error("ElevenLabs Realtime receiver failed ({})", type(exc).__name__)
                await self.push_error(
                    error_msg="ElevenLabs Realtime stopped receiving responses. Please restart the call.",
                    fatal=True,
                )

    async def _handle_evt_error(self, evt):
        error = getattr(evt, "error", None)
        code = getattr(error, "code", None)
        logger.error("ElevenLabs Realtime returned a server error (code={})", code or "unknown")
        await super()._handle_evt_error(evt)

    async def _handle_evt_response_done(self, evt):
        response = getattr(evt, "response", None)
        status = getattr(response, "status", None)
        logger.info(
            "ElevenLabs Realtime response ended (status={}, audio_received={})",
            status or "unknown",
            self._audio_chunk_count,
        )
        if status and status != "completed":
            details = getattr(response, "status_details", None)
            error = getattr(details, "error", None)
            logger.warning(
                "ElevenLabs Realtime response ended with status={} code={}",
                status,
                getattr(error, "code", None) or "unknown",
            )
        if response is None or getattr(response, "usage", None) is not None:
            await super()._handle_evt_response_done(evt)
            return

        # ElevenLabs currently returns usage=null. Pipecat's OpenAI completion
        # handler dereferences usage before emitting its end-of-turn frames,
        # which strands the bot-speaking state and prevents subsequent turns.
        # Finish the real response without inventing token counts.
        await self.stop_processing_metrics()
        if self._current_audio_response is not None:
            await self.push_frame(TTSStoppedFrame())
        await self.push_frame(LLMFullResponseEndFrame())
        self._current_assistant_response = None
        if status == "failed":
            await self.push_error(error_msg="ElevenLabs Realtime could not complete the response.")
            return
        for item in response.output:
            await self._call_event_handler("on_conversation_item_updated", item.id, item)

    async def _handle_evt_audio_delta(self, evt):
        self._audio_chunk_count += 1
        if not self._logged_first_audio_delta:
            self._logged_first_audio_delta = True
            logger.info("ElevenLabs Realtime produced its first audio chunk")
        await super()._handle_evt_audio_delta(evt)

    def _client_secret_session(self) -> dict:
        settings = self._settings
        session_properties = settings.session_properties
        audio = getattr(session_properties, "audio", None)
        input_audio = getattr(audio, "input", None)
        output_audio = getattr(audio, "output", None)
        voice = (
            self._elevenlabs_voice
            or getattr(output_audio, "voice", None)
            or "brian"
        )

        session = {
            "type": "realtime",
            "model": settings.model,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": {"type": "semantic_vad", "eagerness": "medium"},
                },
                "output": {
                    "format": {"type": "audio/pcm"},
                    "voice": voice,
                },
            },
        }
        instructions = getattr(settings, "system_instruction", None)
        if instructions:
            session["instructions"] = instructions

        # Preserve explicit voice selection while keeping the supported audio
        # transport shape fixed to the PCM format expected by Pipecat.
        if input_audio is not None:
            input_turn_detection = getattr(input_audio, "turn_detection", None)
            if input_turn_detection is not None:
                dump = getattr(input_turn_detection, "model_dump", None)
                if callable(dump):
                    detection = dump(exclude_none=True, by_alias=True)
                    if detection:
                        session["audio"]["input"]["turn_detection"] = detection
        return session

    async def _mint_client_secret(self) -> str:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(
                CLIENT_SECRET_URL,
                headers={"xi-api-key": self._elevenlabs_api_key},
                json={
                    "session": self._client_secret_session(),
                    "expires_after": {"anchor": "created_at", "seconds": 600},
                },
            ) as response:
                if response.status >= 400:
                    # Avoid logging the credential or untrusted response body.
                    raise RuntimeError(
                        f"ElevenLabs Realtime credential request failed (HTTP {response.status})"
                    )
                payload = await response.json()

        client_secret = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(client_secret, str) or not client_secret:
            raise RuntimeError("ElevenLabs Realtime returned no client credential")
        return client_secret

    async def _connect(self):
        if self._websocket:
            return

        try:
            client_secret = await self._mint_client_secret()
            socket = await websocket_connect(
                uri=self.base_url,
                additional_headers={"Authorization": f"Bearer {client_secret}"},
            )
            self._websocket = _ElevenLabsProtocolSocket(socket)
            self._receive_task = self.create_task(self._receive_task_handler())
        except Exception as exc:
            logger.error(f"ElevenLabs Realtime connection setup failed: {exc}")
            await self.push_error(
                error_msg=f"ElevenLabs Realtime connection setup failed: {exc}",
                exception=exc,
            )
            self._websocket = None
