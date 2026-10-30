"""What the call sorter hears without words: the beep, and the quiet.

Both providers fork the prospect's audio to us as 8 kHz mu-law. Two facts come from
the samples rather than the transcript:

* SILENCE -- how long the far end has been quiet. A short "Hello." followed by
  quiet is a person waiting for us; a greeting keeps talking.
* THE BEEP -- a mailbox's record tone: one steady sine (typically 400-2000 Hz)
  lasting a few hundred milliseconds. Detected as a run of frames whose energy sits
  almost entirely in one narrow band at a stable frequency. Speech never does that:
  even a held vowel spreads its energy over several harmonics.

Deliberately NOT here: deciding the greeting has ended from silence alone. Real
mailbox-full announcements arrive up to 3.9 s after the greeting's last word
(measured on the fixture set), so "quiet for N seconds" would declare a beep into a
mailbox that is about to say it cannot take a message.

Pure numpy, no I/O, never raises on bad input: a malformed frame is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 8000


def _ulaw_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for byte in range(256):
        value = ~byte & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
        table[byte] = -sample if sign else sample
    return table


_ULAW = _ulaw_table()


def ulaw_to_pcm(frame: bytes) -> np.ndarray:
    """mu-law bytes -> int16 samples (ITU G.711)."""
    if not frame:
        return np.zeros(0, dtype=np.int16)
    return _ULAW[np.frombuffer(frame, dtype=np.uint8)]


def pcm_to_ulaw(samples: np.ndarray) -> bytes:
    """int16 samples -> mu-law bytes. Used by tests to synthesise call audio."""
    pcm = np.clip(samples.astype(np.int32), -32635, 32635)
    sign = np.where(pcm < 0, 0x80, 0)
    magnitude = np.abs(pcm) + 0x84
    exponent = np.floor(np.log2(np.maximum(magnitude, 1))).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


@dataclass(frozen=True)
class Beep:
    frequency_hz: float
    duration_ms: int
    at_seconds: float  # stream time the tone started


class AudioWatcher:
    """Tracks speech/silence and spots a record tone in a mu-law stream.

    `feed` takes frames of any length; analysis runs on 40 ms windows.
    """

    WINDOW = 320  # 40 ms at 8 kHz -> 25 Hz FFT bins
    # Speech floor. Telephone line noise sits well under this; quiet speech above.
    VOICE_RMS = 400.0
    TONE_RMS = 250.0
    # Share of the 200-3400 Hz energy the peak (+-1 bin) must hold to count as a tone.
    TONE_PURITY = 0.70
    TONE_MIN_HZ = 300.0
    TONE_MAX_HZ = 2600.0
    TONE_MAX_DRIFT_HZ = 60.0
    BEEP_MIN_WINDOWS = 4  # 160 ms of steady tone

    def __init__(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        self._samples = 0
        self._last_voice_sample: int | None = None
        self._tone_run = 0
        self._tone_freq = 0.0
        self._tone_start = 0
        self._window = np.hanning(self.WINDOW)
        freqs = np.fft.rfftfreq(self.WINDOW, 1.0 / SAMPLE_RATE)
        self._freqs = freqs
        self._band = (freqs >= 200.0) & (freqs <= 3400.0)
        self.beep: Beep | None = None

    @property
    def seconds(self) -> float:
        return self._samples / SAMPLE_RATE

    @property
    def heard_speech(self) -> bool:
        return self._last_voice_sample is not None

    @property
    def silence_seconds(self) -> float:
        """Quiet since the far end last spoke; 0 before anyone has spoken."""
        if self._last_voice_sample is None:
            return 0.0
        return (self._samples - self._last_voice_sample) / SAMPLE_RATE

    def feed(self, frame: bytes) -> Beep | None:
        """Add audio. Returns a Beep when one completes in this audio, else None."""
        try:
            pcm = ulaw_to_pcm(frame)
        except Exception:  # noqa: BLE001 - malformed audio is ignored, never fatal
            return None
        if pcm.size == 0:
            return None
        self._pending = np.concatenate([self._pending, pcm])
        found: Beep | None = None
        while self._pending.size >= self.WINDOW:
            window = self._pending[: self.WINDOW].astype(np.float64)
            self._pending = self._pending[self.WINDOW:]
            beep = self._analyse(window)
            self._samples += self.WINDOW
            if beep is not None:
                self.beep = beep  # the latest one: a later tone supersedes an earlier
                found = beep
        return found

    def _analyse(self, window: np.ndarray) -> Beep | None:
        rms = float(np.sqrt(np.mean(window * window)))
        is_tone, freq = False, 0.0
        if rms >= self.TONE_RMS:
            spectrum = np.abs(np.fft.rfft(window * self._window)) ** 2
            band = np.where(self._band, spectrum, 0.0)
            total = float(band.sum())
            if total > 0:
                peak = int(np.argmax(band))
                lo, hi = max(peak - 1, 0), min(peak + 2, band.size)
                purity = float(band[lo:hi].sum()) / total
                freq = float(self._freqs[peak])
                is_tone = (
                    purity >= self.TONE_PURITY
                    and self.TONE_MIN_HZ <= freq <= self.TONE_MAX_HZ
                )
        # A tone is not speech: a beep must not reset the silence clock that the
        # person rule reads, or "Hello." + beep-like line noise would look like talk.
        if rms >= self.VOICE_RMS and not is_tone:
            self._last_voice_sample = self._samples + self.WINDOW

        if is_tone and (
            self._tone_run == 0 or abs(freq - self._tone_freq) <= self.TONE_MAX_DRIFT_HZ
        ):
            if self._tone_run == 0:
                self._tone_start = self._samples
                self._tone_freq = freq
            self._tone_run += 1
            if self._tone_run == self.BEEP_MIN_WINDOWS:
                return Beep(
                    frequency_hz=round(self._tone_freq, 1),
                    duration_ms=int(self.BEEP_MIN_WINDOWS * self.WINDOW * 1000 / SAMPLE_RATE),
                    at_seconds=round(self._tone_start / SAMPLE_RATE, 3),
                )
            return None
        self._tone_run = 1 if is_tone else 0
        if is_tone:
            self._tone_start = self._samples
            self._tone_freq = freq
        return None


__all__ = ["AudioWatcher", "Beep", "SAMPLE_RATE", "pcm_to_ulaw", "ulaw_to_pcm"]
