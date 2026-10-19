import asyncio
import hashlib
import io
import struct
import wave

import pytest
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameProcessor

from api.services.pipecat.sandbox_audio_transport import (
    AudioCapture,
    AudioFixture,
    SandboxAudioTransport,
)


def wav_fixture(*, rate=16000, channels=1, width=2, seconds=0.08, amplitude=1000):
    output = io.BytesIO()
    pcm = struct.pack("<h", amplitude) * int(rate * seconds) * channels
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(pcm)
    return output.getvalue(), pcm


@pytest.mark.parametrize("bad", [b"", b"not WAV", None, "not bytes"])
def test_fixture_rejects_non_audio_payloads(bad):
    with pytest.raises(ValueError):
        AudioFixture.from_wav(bad)


@pytest.mark.parametrize(
    "settings",
    [
        {"rate": 8000},
        {"channels": 2},
        {"width": 1},
        {"seconds": 0.01},
    ],
)
def test_fixture_requires_the_explicit_runtime_pcm_format(settings):
    with pytest.raises(ValueError):
        AudioFixture.from_wav(wav_fixture(**settings)[0])


def test_truncated_fixture_cannot_claim_samples_it_does_not_contain():
    encoded, pcm = wav_fixture()
    assert AudioFixture.from_wav(encoded).pcm == pcm
    with pytest.raises(ValueError, match="truncated"):
        AudioFixture.from_wav(encoded[:-30])
    with pytest.raises(ValueError):
        AudioFixture(b"\0")


@pytest.mark.parametrize("value", [0, -1, 121, True, "10", float("inf"), float("nan")])
def test_transport_requires_a_finite_bounded_duration(value):
    with pytest.raises(ValueError):
        SandboxAudioTransport(duration_limit=value)


def test_capture_has_a_hard_memory_limit_and_keeps_pcm_evidence_private():
    capture = AudioCapture(0.04)
    pcm = struct.pack("<h", 1000) * 640
    frame = OutputAudioRawFrame(pcm, 16000, 1)
    capture.append(frame, 1.0)
    with pytest.raises(ValueError, match="reserved capture limit"):
        capture.append(frame, 2.0)
    assert bytes(capture.pcm) == pcm
    assert capture.metadata()["pcm_sha256"] == hashlib.sha256(pcm).hexdigest()
    assert "pcm" not in capture.metadata()
    assert AudioFixture.from_wav(capture.wav_bytes()).pcm == pcm


class EchoAudio(FrameProcessor):
    """Exercise transport frames, not recognition, synthesis or a simulated model."""

    def __init__(self):
        super().__init__()
        self.input_times = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if any(frame.audio):
                self.input_times.append(asyncio.get_running_loop().time())
                await self.push_frame(
                    OutputAudioRawFrame(
                        frame.audio, frame.sample_rate, frame.num_channels
                    )
                )
        else:
            await self.push_frame(frame, direction)


@pytest.mark.asyncio
async def test_native_pipeline_plays_and_captures_pcm_with_real_pacing():
    transport = SandboxAudioTransport(duration_limit=2)
    echo = EchoAudio()
    disconnected = []

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(*_):
        disconnected.append(True)

    task = PipelineTask(
        Pipeline([transport.input(), echo, transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
    )
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    encoded, pcm = wav_fixture()
    try:
        await transport.play_wav(encoded)
        for _ in range(100):
            if len(transport.capture.pcm) >= len(pcm):
                break
            await asyncio.sleep(0.01)
        assert bytes(transport.capture.pcm) == pcm
        assert len(echo.input_times) == 4
        assert all(
            later - earlier >= 0.015
            for earlier, later in zip(echo.input_times, echo.input_times[1:])
        )
        assert transport.failure is None
    finally:
        await task.cancel()
        await asyncio.wait_for(running, timeout=5)
    assert disconnected == [True]


@pytest.mark.asyncio
async def test_duration_limit_ends_the_native_pipeline_without_provider_calls():
    transport = SandboxAudioTransport(duration_limit=0.1)
    task = PipelineTask(
        Pipeline([transport.input(), EchoAudio(), transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
    )
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    try:
        done, _ = await asyncio.wait({running}, timeout=2)
        assert running in done, "Private transport did not enforce its duration cap"
        await running
        assert transport.failure == "Private audio session duration limit reached"
    finally:
        if not running.done():
            await task.cancel()
            await asyncio.wait_for(running, timeout=5)
