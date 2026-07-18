"""Dograh wrapper for ElevenLabsTTSService."""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

_CONNECT_TIMEOUT_S = 8.0
_START_TIMEOUT_S = 15.0


class DograhElevenLabsTTSService(ElevenLabsTTSService):
    """ElevenLabsTTSService with timeouts on start() and _connect_websocket()."""

    async def start(self, frame):
        t0 = time.monotonic()
        logger.info("DograhElevenLabs: start() entry")
        try:
            await asyncio.wait_for(super().start(frame), timeout=_START_TIMEOUT_S)
            logger.info(f"DograhElevenLabs: start() completed in {time.monotonic()-t0:.2f}s")
            # DIAG: one-shot direct HTTP TTS test of the resolved key + voice.
            # Distinguishes an account/key/quota problem (HTTP fails) from a
            # multi-stream websocket integration problem (HTTP works, WS silent).
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=12) as _c:
                    _r = await _c.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{self._settings.voice}",
                        headers={"xi-api-key": self._api_key or ""},
                        json={"text": "test", "model_id": self._settings.model},
                    )
                _info = (
                    f"OK {len(_r.content)} audio bytes"
                    if _r.status_code == 200
                    else _r.text[:300]
                )
                logger.warning(
                    f"[DIAG-EL-HTTP] direct TTS status={_r.status_code} voice={self._settings.voice} {_info}"
                )
            except Exception as _e:
                logger.warning(f"[DIAG-EL-HTTP] direct TTS error: {_e!r}")
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.error(f"DograhElevenLabs: start() TIMED OUT after {elapsed:.1f}s!")
            await self.push_error(
                error_msg=f"ElevenLabs TTS start() timed out ({elapsed:.1f}s). "
                "The ElevenLabs API may be slow or unreachable.",
                fatal=True,
            )

    async def _connect(self):
        logger.info("DograhElevenLabs: _connect() entry")
        await super()._connect()
        logger.info("DograhElevenLabs: _connect() done")

    async def run_tts(self, text: str, context_id: str):
        logger.info(f"[DIAG] DograhElevenLabs: run_tts() called with text len={len(text)}")
        async for frame in super().run_tts(text, context_id):
            yield frame
        logger.info("[DIAG] DograhElevenLabs: run_tts() completed")

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import TTSSpeakFrame
        is_static_speak = isinstance(frame, TTSSpeakFrame)
        if is_static_speak:
            logger.info(f"[DIAG] DograhElevenLabs: process_frame received TTSSpeakFrame len={len(frame.text)}")
        await super().process_frame(frame, direction)
        if is_static_speak:
            # A static TTSSpeakFrame (e.g. the agent's First Message greeting) has
            # no LLMFullResponseEndFrame to trigger generation on the ElevenLabs
            # multi-stream endpoint, so ElevenLabs buffers the text and never
            # renders audio — the greeting comes out silent and the pipeline
            # stalls waiting for it. Force a flush so the static utterance is
            # actually synthesized.
            try:
                await self.flush_audio()
                logger.info("[DIAG] DograhElevenLabs: flushed context after TTSSpeakFrame")
            except Exception as e:
                logger.warning(
                    f"DograhElevenLabs: flush_audio after TTSSpeakFrame failed: {e}"
                )

    async def _connect_websocket(self):
        logger.info(
            f"DograhElevenLabs: _connect_websocket() entry (auto_mode={getattr(self, '_auto_mode', None)})"
        )
        # On the ElevenLabs multi-stream-input endpoint, audio is only generated
        # when a context is CLOSED (on_turn_context_completed -> _close_context)
        # OR when auto_mode is enabled. We were seeing text sent but ZERO audio
        # returned on every turn — the close trigger wasn't firing. Force
        # auto_mode so ElevenLabs generates audio on each text send. This is set
        # before the URL is built in super()._connect_websocket().
        self._auto_mode = True
        try:
            await asyncio.wait_for(super()._connect_websocket(), timeout=_CONNECT_TIMEOUT_S)
            logger.info("DograhElevenLabs: _connect_websocket() succeeded")
        except asyncio.TimeoutError:
            self._websocket = None
            logger.error(
                f"ElevenLabs WebSocket connect timed out after {_CONNECT_TIMEOUT_S}s. "
                "Check ElevenLabs API reachability from this host."
            )
            await self.push_error(
                error_msg=(
                    f"ElevenLabs WebSocket connection timed out ({_CONNECT_TIMEOUT_S}s). "
                    "The ElevenLabs API may be unreachable from this server."
                ),
                fatal=True,
            )
