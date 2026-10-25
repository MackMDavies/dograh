"""Delegating mute-strategy wrapper used only by isolated voice-test runs."""

import asyncio

from pipecat.frames.frames import Frame
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy


class ObservedUserMuteStrategy(BaseUserMuteStrategy):
    """Preserve a strategy's result while recording its bounded test evidence."""

    def __init__(self, delegate, source, timing_evidence, *, reason=None):
        super().__init__()
        self._delegate = delegate
        self._source = source
        self._timing_evidence = timing_evidence
        self._reason = reason

    async def setup(self, task_manager):
        await self._delegate.setup(task_manager)

    async def cleanup(self):
        await self._delegate.cleanup()

    async def process_frame(self, frame: Frame) -> bool:
        muted = await self._delegate.process_frame(frame)
        reason = self._reason() if self._reason is not None else None
        self._timing_evidence.record_strategy_mute(
            asyncio.get_running_loop().time(), self._source, muted, reason
        )
        return muted
