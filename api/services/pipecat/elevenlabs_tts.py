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
        if isinstance(frame, TTSSpeakFrame):
            logger.info(f"[DIAG] DograhElevenLabs: process_frame received TTSSpeakFrame len={len(frame.text)}")
        await super().process_frame(frame, direction)

    async def _connect_websocket(self):
        logger.info("DograhElevenLabs: _connect_websocket() entry")
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
