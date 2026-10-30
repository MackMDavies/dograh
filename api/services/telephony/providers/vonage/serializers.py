"""Vonage frame serializer: pipecat's, plus the transfer hand-off pipecat leaves out."""

from typing import Optional

from pipecat.frames.frames import CancelFrame, EndFrame, Frame
from pipecat.serializers.call_strategies import TransferStrategy
from pipecat.serializers.vonage import VonageFrameSerializer as _PipecatVonageFrameSerializer
from pipecat.utils.enums import EndTaskReason


class VonageFrameSerializer(_PipecatVonageFrameSerializer):
    """When the pipeline ends for a transfer, move the caller into the transfer's
    conversation instead of hanging up (as the Twilio and Telnyx serializers do)."""

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
                {
                    "call_uuid": self._call_uuid,
                    "application_id": self._application_id,
                    "private_key": self._private_key,
                }
            )
            if moved:
                self._hangup_attempted = True  # the caller is with the colleague now
                return None
        return await super().serialize(frame)


__all__ = ["VonageFrameSerializer"]
