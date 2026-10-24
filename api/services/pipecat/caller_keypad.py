"""Caller key presses ("press 1 to confirm") reaching the agent.

Every carrier serializer already turns a caller's key press into an InputDTMFFrame, but
nothing in the pipeline consumed it, so a caller pressing 1 was ignored. This processor
sits directly after transport.input() and collects the presses into one entry: it is
complete when the caller presses # or stops pressing for a moment. The entry then goes
to the engine's handler, which records it in the call's history and hands it to the
model; a frame the handler returns is pushed downstream (the cascaded pipeline's user
aggregator picks it up).

Asterisk (ARI) calls report presses as ARI events in the ari_manager process instead,
which reach this processor through inject_key().

Until the engine installs a handler the processor drops key presses, exactly as the
pipeline did before it existed.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from loguru import logger

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import CancelFrame, EndFrame, Frame, InputDTMFFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

VALID_KEYS = frozenset("0123456789*#")
TERMINATION_KEY = "#"
# Long enough to type a date or an account number; anything longer is cut off.
MAX_KEYS = 32

KeysHandler = Callable[[str], Awaitable[Optional[Frame]]]


class CallerKeypadProcessor(FrameProcessor):
    def __init__(self, *, idle_timeout: float = 2.0, **kwargs):
        super().__init__(**kwargs)
        self._idle_timeout = idle_timeout
        self._keys = ""
        self._key_event = asyncio.Event()
        self._idle_task: Optional[asyncio.Task] = None
        self._handler: Optional[KeysHandler] = None
        self._accepting: Callable[[], bool] = lambda: True

    def set_handler(
        self, handler: Optional[KeysHandler], accepting: Optional[Callable[[], bool]] = None
    ) -> None:
        """handler=None turns the processor off (presses are dropped)."""
        self._handler = handler
        self._accepting = accepting or (lambda: True)

    async def inject_key(self, key: str) -> None:
        """A press reported outside the media stream (Asterisk ARI events)."""
        if key in VALID_KEYS:
            await self.queue_frame(InputDTMFFrame(button=KeypadEntry(key)))

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._stop_idle_task()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputDTMFFrame):
            await self._on_key(str(frame.button.value))
            return
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            if not self._idle_task:
                self._idle_task = self.create_task(self._idle_flusher())
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            # The call is ending: an unfinished entry has nobody left to answer it.
            self._keys = ""
            await self._stop_idle_task()
        await self.push_frame(frame, direction)

    async def _on_key(self, key: str) -> None:
        if self._handler is None or key not in VALID_KEYS:
            return
        if not self._accepting():
            logger.debug(f"caller key {key!r} ignored: the agent cannot be interrupted now")
            return
        if not self._keys:
            # Stop the agent mid-sentence, as speaking over it would.
            await self.broadcast_interruption()
        if len(self._keys) < MAX_KEYS:
            self._keys += key
        if key == TERMINATION_KEY:
            await self._flush()
        else:
            self._key_event.set()

    async def _stop_idle_task(self) -> None:
        if self._idle_task:
            task, self._idle_task = self._idle_task, None
            await self.cancel_task(task)

    async def _idle_flusher(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._key_event.wait(), timeout=self._idle_timeout)
                self._key_event.clear()
            except asyncio.TimeoutError:
                await self._flush()

    async def _flush(self) -> None:
        keys, self._keys = self._keys, ""
        if not keys or self._handler is None:
            return
        try:
            frame = await self._handler(keys)
        except Exception as exc:  # noqa: BLE001 -- a key press must never break the call
            logger.error(f"caller keypad handler failed: {exc}")
            return
        if frame is not None:
            await self.push_frame(frame)
