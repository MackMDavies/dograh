"""Plivo frame serializer: pipecat's, plus the transfer hand-off pipecat leaves out."""

from typing import Optional

from pipecat.frames.frames import CancelFrame, EndFrame, Frame
from pipecat.serializers.call_strategies import TransferStrategy
from pipecat.serializers.plivo import PlivoFrameSerializer as _PipecatPlivoFrameSerializer
from pipecat.utils.enums import EndTaskReason


class PlivoFrameSerializer(_PipecatPlivoFrameSerializer):
    """When the pipeline ends for a transfer, move the caller into the transfer's
    conference instead of hanging up (as the Twilio and Telnyx serializers do)."""

    def __init__(self, *args, transfer_strategy: Optional[TransferStrategy] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._transfer_strategy = transfer_strategy
        self._transfer_attempted = False

    async def serialize(self, frame: Frame):
        if (
            isinstance(frame, (EndFrame, CancelFrame))
            and getattr(frame, "reason", None) == EndTaskReason.TRANSFER_CALL.value
            and self._transfer_strategy is not None
            and not self._transfer_attempted
        ):
            self._transfer_attempted = True
            moved = await self._transfer_strategy.execute_transfer(
                {"call_id": self._call_id, "auth_id": self._auth_id, "auth_token": self._auth_token}
            )
            if moved:
                self._hangup_attempted = True  # the caller is with the colleague now
                return None
        return await super().serialize(frame)


__all__ = ["PlivoFrameSerializer"]
