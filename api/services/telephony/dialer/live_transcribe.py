"""Transcribing a call while it is still happening.

The tap already carries the audio; this feeds the same frames to Deepgram's streaming
endpoint and publishes what comes back, so a manager listening in sees the words as well
as hearing them.

DELIBERATELY SEPARATE FROM THE POST-CALL TRANSCRIPT. dialer-call-transcribe runs against
the finished stereo recording, gets speaker separation from the two channels, and is the
record of what was said. This is mono -- the tap mixes both directions into one stream --
so it cannot reliably say who spoke. It is a live view, not a record, and nothing is
stored from it.

Never raises into the call path. A missing API key, a Deepgram outage or a malformed
frame costs the live captions and nothing else: the call continues, the tap keeps
relaying audio, and the real transcript is produced afterwards regardless.
"""

import asyncio
import json
import os
from contextlib import suppress
from typing import Awaitable, Callable

import websockets
from loguru import logger

# mulaw at 8k is what the tap sends. Saying so explicitly matters: Deepgram will happily
# accept the bytes under a wrong encoding and return confident nonsense.
_DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=mulaw&sample_rate=8000&channels=1"
    "&model=nova-2&punctuate=true&smart_format=true"
    # Interim results are what make this feel live rather than arriving in paragraphs,
    # and endpointing closes a sentence on a natural pause rather than a fixed timer.
    "&interim_results=true&endpointing=300"
)

_CONNECT_TIMEOUT_SECONDS = 8.0


class LiveTranscriber:
    """Feeds tap audio to Deepgram and hands finished text to a callback.

    Used as an async context manager. Entering connects; if it cannot, the transcriber
    stays disabled and `feed` becomes a no-op, so the caller needs no special case for
    "transcription is not available".
    """

    def __init__(self, call_id: str, on_text: Callable[[dict], Awaitable[None]]):
        self._call_id = call_id
        self._on_text = on_text
        self._socket: websockets.ClientConnection | None = None
        self._reader: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._socket is not None

    async def __aenter__(self) -> "LiveTranscriber":
        api_key = (os.environ.get("DEEPGRAM_API_KEY") or "").strip()
        if not api_key:
            # Said once per call rather than per frame, and at info: an unset key is a
            # deployment that has not turned this on, not a fault.
            logger.info(
                f"live transcription off for {self._call_id}: DEEPGRAM_API_KEY is unset"
            )
            return self
        try:
            self._socket = await asyncio.wait_for(
                websockets.connect(
                    _DEEPGRAM_URL,
                    additional_headers={"Authorization": f"Token {api_key}"},
                ),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
            self._reader = asyncio.create_task(self._read())
            logger.info(f"live transcription open for {self._call_id}")
        except Exception as exc:  # noqa: BLE001 - a live call is on this path
            logger.warning(f"live transcription unavailable for {self._call_id}: {exc}")
            self._socket = None
        return self

    async def __aexit__(self, *_exc) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            with suppress(Exception):
                # Tells Deepgram to flush and close rather than dropping the connection,
                # so the last thing said is not lost.
                await socket.send(json.dumps({"type": "CloseStream"}))
            with suppress(Exception):
                await socket.close()
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None

    async def feed(self, frame: bytes) -> None:
        """Send one audio frame. Silently does nothing when transcription is off."""
        socket = self._socket
        if socket is None or not frame:
            return
        try:
            await socket.send(frame)
        except Exception as exc:  # noqa: BLE001
            # One failure means the socket is gone. Disabling here stops a dead
            # connection generating a warning for every 20ms frame of a long call.
            logger.warning(f"live transcription stopped for {self._call_id}: {exc}")
            self._socket = None

    async def _read(self) -> None:
        """Publish transcripts as they arrive."""
        socket = self._socket
        if socket is None:
            return
        try:
            async for message in socket:
                if not isinstance(message, (str, bytes, bytearray)):
                    continue
                try:
                    payload = json.loads(message)
                except Exception:  # noqa: BLE001
                    continue
                alternatives = (payload.get("channel") or {}).get("alternatives") or []
                text = (alternatives[0].get("transcript") if alternatives else "") or ""
                if not text.strip():
                    # Deepgram emits empty results constantly during silence. Publishing
                    # them would clear the caption every time nobody is speaking.
                    continue
                await self._on_text(
                    {
                        "type": "transcript",
                        "text": text,
                        "is_final": bool(payload.get("is_final")),
                        "start": payload.get("start"),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"live transcription reader ended for {self._call_id}: {exc}")
