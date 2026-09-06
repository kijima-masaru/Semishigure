"""WAV loading / synthesis for RTP playout.

Sources are pre-encoded to every codec we may negotiate so that the pump does
byte slicing only. 8 kHz / 16-bit / mono is required; 16/32/44.1/48 kHz mono
input is decimated with a simple low-pass + linear interpolation so test
files do not need pre-conversion. Stereo is downmixed.
"""

from __future__ import annotations

import array
import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

from semishigure.media import codec as g711

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20 ms
FRAME_MS = 20


@dataclass
class AudioSource:
    """Pre-encoded audio, one byte per sample, keyed by codec name."""

    name: str
    pcm16: bytes
    encoded: dict[str, bytes] = field(default_factory=dict)
    loop: bool = True

    @property
    def samples(self) -> int:
        return len(self.pcm16) // 2

    @property
    def duration_s(self) -> float:
        return self.samples / SAMPLE_RATE

    def payload(self, codec: str) -> bytes:
        if codec not in self.encoded:
            self.encoded[codec] = g711.encode(codec, self.pcm16)
        return self.encoded[codec]


def load_wav(path: str | Path, loop: bool = True) -> AudioSource:
    path = Path(path)
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported (got {width * 8}-bit)")
    samples = array.array("h")
    samples.frombytes(frames)
    if channels > 1:
        mono = array.array("h", (int(sum(samples[i : i + channels]) / channels) for i in range(0, len(samples), channels)))
        samples = mono
    if rate != SAMPLE_RATE:
        samples = _resample(samples, rate, SAMPLE_RATE)
    # pad to a whole number of 20 ms frames so looping stays frame aligned
    rem = len(samples) % FRAME_SAMPLES
    if rem:
        samples.extend([0] * (FRAME_SAMPLES - rem))
    src = AudioSource(name=path.name, pcm16=samples.tobytes(), loop=loop)
    for c in ("PCMU", "PCMA"):
        src.payload(c)
    return src


def _resample(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    if src_rate % dst_rate == 0:
        factor = src_rate // dst_rate
        # box-filter low-pass then decimate
        out = array.array("h")
        n = len(samples) // factor
        for i in range(n):
            chunk = samples[i * factor : (i + 1) * factor]
            out.append(int(sum(chunk) / factor))
        return out
    # generic linear interpolation
    ratio = src_rate / dst_rate
    n = int(len(samples) / ratio)
    out = array.array("h")
    for i in range(n):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = samples[j]
        b = samples[j + 1] if j + 1 < len(samples) else a
        out.append(int(a + (b - a) * frac))
    return out


def synth_speech_like(seconds: float, base_hz: float = 180.0, seed: int = 1, level: float = 0.35) -> AudioSource:
    """Deterministic 'speech-like' test signal: a harmonic voice with syllable
    envelopes and pauses. Not intelligible, but clearly not silence and each
    seed produces a distinguishable pattern."""
    rnd = _Lcg(seed)
    total = int(seconds * SAMPLE_RATE)
    out = array.array("h", bytes(total * 2))
    i = 0
    while i < total:
        syllable = int(SAMPLE_RATE * (0.08 + rnd.next() * 0.22))
        pause = int(SAMPLE_RATE * (0.02 + rnd.next() * 0.12))
        f0 = base_hz * (0.85 + rnd.next() * 0.4)
        formant = 500 + rnd.next() * 1500
        for n in range(syllable):
            if i >= total:
                break
            t = n / SAMPLE_RATE
            env = math.sin(math.pi * n / syllable)
            v = (
                math.sin(2 * math.pi * f0 * t)
                + 0.5 * math.sin(2 * math.pi * 2 * f0 * t)
                + 0.3 * math.sin(2 * math.pi * 3 * f0 * t)
                + 0.4 * math.sin(2 * math.pi * formant * t)
            )
            out[i] = int(max(-1.0, min(1.0, v * env * level)) * 32767)
            i += 1
        i += pause
    src = AudioSource(name=f"synth-{seed}", pcm16=out.tobytes(), loop=True)
    for c in ("PCMU", "PCMA"):
        src.payload(c)
    return src


def write_wav(path: str | Path, pcm16: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm16)


class _Lcg:
    def __init__(self, seed: int):
        self.state = (seed * 2654435761) & 0xFFFFFFFF or 1

    def next(self) -> float:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state / 0x7FFFFFFF


def frame_count(source: AudioSource) -> int:
    return source.samples // FRAME_SAMPLES


def rms_frame(pcm16: bytes) -> float:
    n = len(pcm16) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack(f"<{n}h", pcm16[: n * 2])
    return math.sqrt(sum(v * v for v in vals) / n)
