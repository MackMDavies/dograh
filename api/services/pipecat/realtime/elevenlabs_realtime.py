"""ElevenLabs Realtime adapter using Pipecat's OpenAI-compatible wire client.

ElevenLabs Realtime implements the OpenAI Realtime event protocol at its own
WebSocket endpoint. We mint a short-lived credential server-side for each
connection so the long-lived account API key never leaves Dograh.
"""

from __future__ import annotations

import aiohttp
from loguru import logger

from api.services.pipecat.realtime.openai_realtime import (
    DograhOpenAIRealtimeLLMService,
)


CLIENT_SECRET_URL = "https://api.elevenlabs.io/v1/convai/realtime/client_secrets"
REALTIME_WEBSOCKET_URL = "wss://api.elevenlabs.io/v1/convai/realtime"


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
            # Pipecat's upstream transport sends Authorization: Bearer <api_key>.
            # Supply only the short-lived client secret for this socket.
            account_api_key = self.api_key
            self.api_key = client_secret
            try:
                await super()._connect()
            finally:
                self.api_key = account_api_key
        except Exception as exc:
            logger.error(f"ElevenLabs Realtime connection setup failed: {exc}")
            await self.push_error(
                error_msg=f"ElevenLabs Realtime connection setup failed: {exc}",
                exception=exc,
            )
            self._websocket = None
