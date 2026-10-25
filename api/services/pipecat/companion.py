"""Three-way calls on stream carriers: the colleague joins, the agent stays on.

On Twilio, Telnyx, Vonage, Plivo, Vobiz and Cloudonix the agent's audio is one media
stream on the caller's leg, and putting the caller in a carrier conference ends it.
So Dograh does the mixing itself. The colleague is dialled as a second call whose
media stream connects to /telephony/companion/{transfer_id} (companion_ws.py) — on
whichever API worker accepts it — and audio moves between that socket and the call's
pipeline over Redis in ~20 ms chunks:

    companion:{id}:up    the colleague's voice      (socket → pipeline)
    companion:{id}:down  the caller + the agent     (pipeline → socket)
    companion:{id}:ctl   "end" (call over) / "left" (colleague hung up)

In the pipeline, CompanionMixer (wrapping the transport's existing output mixer, which
runs every output chunk in real time) adds the colleague to what the caller hears and
sends caller + agent to the colleague; CompanionInputProcessor adds the colleague to
what the agent hears. With no colleague both pass audio through untouched.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Awaitable, Callable, Optional

import numpy as np
from loguru import logger

from pipecat.audio.mixers.base_audio_mixer import BaseAudioMixer
from pipecat.frames.frames import Frame, InputAudioRawFrame, MixerControlFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Never let a slow side build up more than this much audio (latency guard).
MAX_BUFFER_SECONDS = 0.4


def up_channel(transfer_id: str) -> str:
    return f"companion:{transfer_id}:up"


def down_channel(transfer_id: str) -> str:
    return f"companion:{transfer_id}:down"


def control_channel(transfer_id: str) -> str:
    return f"companion:{transfer_id}:ctl"


def add_pcm(a: bytes, b: bytes) -> bytes:
    """Sum two 16-bit mono PCM chunks (the shorter is padded with silence)."""
    if not b:
        return a
    if not a:
        return b
    x = np.frombuffer(a, dtype=np.int16).astype(np.int32)
    y = np.frombuffer(b, dtype=np.int16).astype(np.int32)
    if len(x) < len(y):
        x = np.pad(x, (0, len(y) - len(x)))
    elif len(y) < len(x):
        y = np.pad(y, (0, len(x) - len(y)))
    return np.clip(x + y, -32768, 32767).astype(np.int16).tobytes()


class _PcmBuffer:
    def __init__(self, sample_rate: int):
        self._data = bytearray()
        self._cap = int(sample_rate * 2 * MAX_BUFFER_SECONDS)

    def push(self, pcm: bytes) -> None:
        self._data.extend(pcm)
        if len(self._data) > self._cap:
            del self._data[: len(self._data) - self._cap]

    def take(self, nbytes: int) -> bytes:
        chunk = bytes(self._data[:nbytes])
        del self._data[:nbytes]
        return chunk


class CompanionBridge:
    """One live three-way call, on the worker running the caller's pipeline."""

    def __init__(
        self,
        transfer_id: str,
        sample_rate: int,
        redis_url: str,
        on_left: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.transfer_id = transfer_id
        self._rate = sample_rate
        self._redis_url = redis_url
        self._on_left = on_left
        self._colleague_for_caller = _PcmBuffer(sample_rate)
        self._colleague_for_agent = _PcmBuffer(sample_rate)
        self._caller_for_colleague = _PcmBuffer(sample_rate)
        self._outgoing: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._tasks: list[asyncio.Task] = []
        self._redis = None
        self.active = False

    async def start(self) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(self._redis_url)
        self.active = True
        self._tasks = [
            asyncio.create_task(self._listen(), name=f"companion-up-{self.transfer_id}"),
            asyncio.create_task(self._send(), name=f"companion-down-{self.transfer_id}"),
        ]

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(up_channel(self.transfer_id), control_channel(self.transfer_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                channel = message["channel"]
                channel = channel.decode() if isinstance(channel, bytes) else channel
                if channel.endswith(":ctl"):
                    if message["data"] in (b"left", "left"):
                        await self._colleague_left()
                        return
                    continue
                pcm = message["data"]
                self._colleague_for_caller.push(pcm)
                self._colleague_for_agent.push(pcm)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 -- the caller's call must go on
            logger.warning(f"[companion {self.transfer_id}] listener stopped: {e}")
        finally:
            with suppress(Exception):
                await pubsub.aclose()

    async def _send(self) -> None:
        try:
            while True:
                pcm = await self._outgoing.get()
                await self._redis.publish(down_channel(self.transfer_id), pcm)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[companion {self.transfer_id}] sender stopped: {e}")

    async def _colleague_left(self) -> None:
        self.active = False
        logger.info(f"[companion {self.transfer_id}] the colleague left the call")
        if self._on_left:
            with suppress(Exception):
                await self._on_left()

    def on_caller_input(self, pcm: bytes) -> bytes:
        """Caller audio in: keep it for the colleague; give the agent caller + colleague."""
        if not self.active:
            return pcm
        self._caller_for_colleague.push(pcm)
        return add_pcm(pcm, self._colleague_for_agent.take(len(pcm)))

    def on_output_chunk(self, agent_pcm: bytes, mixed: bytes) -> bytes:
        """One real-time output chunk: the caller hears agent + colleague; the colleague
        is sent agent + caller."""
        if not self.active:
            return mixed
        for_colleague = add_pcm(agent_pcm, self._caller_for_colleague.take(len(agent_pcm)))
        with suppress(asyncio.QueueFull):
            self._outgoing.put_nowait(for_colleague)
        return add_pcm(mixed, self._colleague_for_caller.take(len(mixed)))

    async def stop(self) -> None:
        """The call is over: tell the colleague's socket to hang up."""
        self.active = False
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.publish(control_channel(self.transfer_id), b"end")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(BaseException):
                await task
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.aclose()


class CompanionMixer(BaseAudioMixer):
    """The transport's output mixer, plus the colleague when a bridge is attached."""

    def __init__(self, inner: Optional[BaseAudioMixer]):
        super().__init__()
        self._inner = inner
        self.bridge: Optional[CompanionBridge] = None

    async def start(self, sample_rate: int):
        if self._inner:
            await self._inner.start(sample_rate)

    async def stop(self):
        if self._inner:
            await self._inner.stop()

    async def process_frame(self, frame: MixerControlFrame):
        if self._inner:
            await self._inner.process_frame(frame)

    async def mix(self, audio: bytes) -> bytes:
        mixed = await self._inner.mix(audio) if self._inner else audio
        bridge = self.bridge
        if bridge is not None:
            try:
                return bridge.on_output_chunk(audio, mixed)
            except Exception as e:  # noqa: BLE001 -- never break the caller's audio
                logger.warning(f"companion mix failed: {e}")
        return mixed


class CompanionInputProcessor(FrameProcessor):
    """Adds the colleague's voice to the caller audio the agent hears."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bridge: Optional[CompanionBridge] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        bridge = self.bridge
        if bridge is not None and isinstance(frame, InputAudioRawFrame):
            try:
                frame.audio = bridge.on_caller_input(frame.audio)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"companion input mix failed: {e}")
        await self.push_frame(frame, direction)


def install_companion_mixer(transport) -> Optional[CompanionMixer]:
    """Wrap the transport's output mixer so a colleague can be mixed in later.
    Returns None for transports without one (web calls); they get no three-way."""
    params = getattr(transport, "_params", None)
    if params is None or not hasattr(params, "audio_out_mixer"):
        return None
    inner = params.audio_out_mixer
    if isinstance(inner, CompanionMixer):
        return inner
    if not isinstance(inner, BaseAudioMixer):
        # No mixer (or a per-destination mapping): adding one would change how this
        # transport paces audio, so it gets no three-way rather than a riskier path.
        return None
    mixer = CompanionMixer(inner)
    params.audio_out_mixer = mixer
    return mixer
