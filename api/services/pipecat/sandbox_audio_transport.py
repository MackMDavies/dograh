"""Bounded PCM transport for private, headless runs of the real voice pipeline.

This transport plays supplied WAV fixtures through native audio input and retains
native audio output in memory. It does not generate transcripts, replace speech
providers, dial a number, fetch a URL, or publish recordings. Admission, durable
jobs and authorized evidence storage belong to the test-session service.
"""

import asyncio
import hashlib
import io
import math
import wave
from dataclasses import dataclass

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

SAMPLE_RATE = 16_000
CHUNK_SECONDS = 0.02
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_SECONDS) * 2


@dataclass(frozen=True)
class AudioFixture:
    pcm: bytes

    def __post_init__(self):
        if (
            not isinstance(self.pcm, bytes)
            or len(self.pcm) % 2
            or not CHUNK_BYTES <= len(self.pcm) <= SAMPLE_RATE * 2 * 30
        ):
            raise ValueError(
                "A fixture requires 20 milliseconds to 30 seconds of complete PCM samples"
            )

    @classmethod
    def from_wav(cls, encoded: bytes) -> "AudioFixture":
        if not isinstance(encoded, bytes) or len(encoded) > SAMPLE_RATE * 2 * 30 + 4096:
            raise ValueError("A fixture must be a WAV file no longer than 30 seconds")
        try:
            with wave.open(io.BytesIO(encoded), "rb") as stream:
                if (
                    stream.getnchannels(),
                    stream.getsampwidth(),
                    stream.getframerate(),
                    stream.getcomptype(),
                ) != (1, 2, SAMPLE_RATE, "NONE"):
                    raise ValueError("Fixtures require mono 16-bit PCM at 16000 Hz")
                frames = stream.getnframes()
                if (
                    frames < int(SAMPLE_RATE * CHUNK_SECONDS)
                    or frames > SAMPLE_RATE * 30
                ):
                    raise ValueError(
                        "Fixture duration must be between 20 milliseconds and 30 seconds"
                    )
                pcm = stream.readframes(frames)
                if len(pcm) != frames * 2:
                    raise ValueError("The WAV fixture is truncated")
        except (wave.Error, EOFError) as exc:
            raise ValueError("A valid uncompressed WAV fixture is required") from exc
        return cls(pcm)


class AudioCapture:
    def __init__(self, duration_limit: float):
        self.limit_bytes = int(SAMPLE_RATE * 2 * duration_limit)
        self.pcm = bytearray()
        self.first_frame_at: float | None = None

    def append(self, frame: OutputAudioRawFrame, at: float) -> None:
        if (
            frame.sample_rate != SAMPLE_RATE
            or frame.num_channels != 1
            or len(frame.audio) % 2
        ):
            raise ValueError("Runtime test output must be mono 16-bit PCM at 16000 Hz")
        if len(self.pcm) + len(frame.audio) > self.limit_bytes:
            raise ValueError("Private test audio exceeded its reserved capture limit")
        if frame.audio and self.first_frame_at is None:
            self.first_frame_at = at
        self.pcm.extend(frame.audio)

    def wav_bytes(self) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(SAMPLE_RATE)
            stream.writeframes(bytes(self.pcm))
        return output.getvalue()

    def metadata(self) -> dict:
        return {
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "pcm_bytes": len(self.pcm),
            "duration_seconds": len(self.pcm) / (SAMPLE_RATE * 2),
            "pcm_sha256": hashlib.sha256(self.pcm).hexdigest(),
            "measurement_scope": "runtime PCM transport; excludes carrier and network latency",
        }


class _FixtureInput(BaseInputTransport):
    def __init__(self, owner: "SandboxAudioTransport", params: TransportParams):
        super().__init__(params)
        self.owner = owner
        self.started = asyncio.Event()
        self.closed = False
        self.stream_task = None
        self.play_lock = asyncio.Lock()
        self.active: tuple[bytes, asyncio.Future] | None = None
        self.cursor = 0
        self.deadline = 0.0

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        self.deadline = asyncio.get_running_loop().time() + self.owner.duration_limit
        self.stream_task = self.create_task(self._stream())
        self.started.set()
        await self.owner._call_event_handler("on_client_connected", None)

    async def play(self, fixture: AudioFixture) -> None:
        await asyncio.wait_for(self.started.wait(), timeout=5)
        async with self.play_lock:
            if self.closed:
                raise RuntimeError("The private audio session is closed")
            if (
                asyncio.get_running_loop().time() + len(fixture.pcm) / (SAMPLE_RATE * 2)
                > self.deadline
            ):
                raise ValueError(
                    "The fixture does not fit in the remaining audio budget"
                )
            done = asyncio.get_running_loop().create_future()
            self.active = (fixture.pcm, done)
            self.cursor = 0
            try:
                await done
            finally:
                self.active = None

    async def _stream(self):
        loop = asyncio.get_running_loop()
        while not self.closed:
            if loop.time() >= self.deadline:
                self.owner.failure = "Private audio session duration limit reached"
                await self.push_frame(
                    ErrorFrame(self.owner.failure, fatal=True), FrameDirection.UPSTREAM
                )
                return
            active = self.active
            frame_started_at = loop.time()
            audio = (
                active[0][self.cursor : self.cursor + CHUNK_BYTES]
                if active
                else bytes(CHUNK_BYTES)
            )
            audio = audio.ljust(CHUNK_BYTES, b"\0")
            await self.push_audio_frame(InputAudioRawFrame(audio, SAMPLE_RATE, 1))
            if active:
                self.cursor += CHUNK_BYTES
            await asyncio.sleep(max(0, frame_started_at + CHUNK_SECONDS - loop.time()))
            if active and self.cursor >= len(active[0]) and not active[1].done():
                # Clear before waking the sender so trailing silence cannot
                # replay the last fixture while it awaits the next response.
                self.active = None
                active[1].set_result(None)

    async def _close(self):
        if self.closed:
            return
        self.closed = True
        if self.active and not self.active[1].done():
            self.active[1].set_exception(
                RuntimeError("The private audio session ended during fixture playback")
            )
        if self.stream_task and self.stream_task is not asyncio.current_task():
            await self.cancel_task(self.stream_task)
        await self.owner._call_event_handler("on_client_disconnected", None)

    async def stop(self, frame: EndFrame):
        await self._close()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await self._close()
        await super().cancel(frame)

    async def cleanup(self):
        await self._close()
        await super().cleanup()


class _CapturedOutput(BaseOutputTransport):
    def __init__(self, owner: "SandboxAudioTransport", params: TransportParams):
        super().__init__(params)
        self.owner = owner

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if self.owner.voice_observer is not None:
            self.owner.voice_observer.observe_output(frame, direction)
        await super().push_frame(frame, direction)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        try:
            at = asyncio.get_running_loop().time()
            self.owner.capture.append(frame, at)
            if self.owner.voice_observer is not None:
                self.owner.voice_observer.record_audio(at, len(frame.audio))
        except ValueError as exc:
            self.owner.failure = str(exc)
            return False
        # Native MediaSender must experience the same PCM playback pacing as
        # a live transport; collecting bytes instantly would falsify timing.
        await asyncio.sleep(len(frame.audio) / (SAMPLE_RATE * 2))
        return True


class SandboxAudioTransport(BaseTransport):
    """Use with an attested sandbox run and the standard voice pipeline only."""

    def __init__(self, *, duration_limit: float):
        if (
            isinstance(duration_limit, bool)
            or not isinstance(duration_limit, (int, float))
            or not math.isfinite(duration_limit)
            or not 0 < duration_limit <= 120
        ):
            raise ValueError(
                "A finite reserved audio duration between zero and 120 seconds is required"
            )
        super().__init__()
        self.duration_limit = duration_limit
        self.failure: str | None = None
        self.voice_observer = None
        self.capture = AudioCapture(duration_limit)
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_end_silence_secs=0,
            audio_out_auto_silence=False,
        )
        self._input = _FixtureInput(self, params)
        self._output = _CapturedOutput(self, params)

    def input(self):
        return self._input

    def output(self):
        return self._output

    def set_voice_observer(self, observer):
        if (
            self.voice_observer is not None
            or self._input.started.is_set()
            or observer.output is not self._output
        ):
            raise ValueError(
                "A private voice observer must bind once before the pipeline starts"
            )
        self.voice_observer = observer

    async def play_wav(self, encoded: bytes) -> None:
        await self._input.play(AudioFixture.from_wav(encoded))
