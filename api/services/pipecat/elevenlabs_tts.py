"""Dograh wrapper for ElevenLabsTTSService.

ElevenLabs' WebSocket connect has no timeout in pipecat's base class.
If the ElevenLabs API is unreachable or slow to respond, `_connect_websocket`
hangs indefinitely, which blocks the pipeline StartFrame from completing.
The pipeline event loop keeps running (heartbeat fires) but no frames ever
flow because start() never returns — resulting in total silence with no error.

This wrapper enforces an 8-second timeout on the WebSocket connect so the
pipeline fails fast with a clear error instead of hanging forever.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

_CONNECT_TIMEOUT_S = 8.0


class DograhElevenLabsTTSService(ElevenLabsTTSService):
    """ElevenLabsTTSService with a hard timeout on WebSocket connect."""

    async def _connect_websocket(self):
        try:
            await asyncio.wait_for(super()._connect_websocket(), timeout=_CONNECT_TIMEOUT_S)
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
