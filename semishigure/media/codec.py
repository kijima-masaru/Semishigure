"""G.711 mu-law / A-law codecs implemented with lookup tables (no audioop).

Encoding tables are built once at import time. Whole buffers are converted
with bytes.translate / array tricks so the per-packet cost is negligible;
the media pump only slices pre-encoded audio.
"""

from __future__ import annotations

import array
import struct

BIAS = 0x84
CLIP = 32635


def _linear_to_ulaw(sample: int) -> int:
    sign = 0
    if sample < 0:
        sign = 0x80
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _linear_to_alaw(sample: int) -> int:
    sign = 0x80 if sample >= 0 else 0
    if sample < 0:
        sample = -sample - 1  # matches G.711 reference (ITU-T) behaviour
    if sample > 32767:
        sample = 32767
    if sample >= 256:
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not (sample & mask):
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        value = (exponent << 4) | mantissa
    else:
        value = sample >> 4
    return (value | sign) ^ 0x55


def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + BIAS) << exponent
    sample -= BIAS
    return -sample if sign else sample


def _alaw_to_linear(a: int) -> int:
    a ^= 0x55
    sign = a & 0x80
    exponent = (a >> 4) & 0x07
    mantissa = a & 0x0F
    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)
    return sample if sign else -sample


# 14-bit index tables (sample >> 2) -> encoded byte; 16384 entries each.
_ULAW_ENC = bytes(_linear_to_ulaw((i - 8192) << 2) for i in range(16384))
_ALAW_ENC = bytes(_linear_to_alaw((i - 8192) << 2) for i in range(16384))
_ULAW_DEC = array.array("h", (_ulaw_to_linear(i) for i in range(256)))
_ALAW_DEC = array.array("h", (_alaw_to_linear(i) for i in range(256)))


def _encode(pcm16: bytes, table: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
    # (s >> 2) + 8192 maps -32768..32767 to 0..16383
    return bytes(table[(s >> 2) + 8192] for s in samples)


def pcm16_to_ulaw(pcm16: bytes) -> bytes:
    return _encode(pcm16, _ULAW_ENC)


def pcm16_to_alaw(pcm16: bytes) -> bytes:
    return _encode(pcm16, _ALAW_ENC)


def _decode(data: bytes, table: array.array) -> bytes:
    out = array.array("h", (table[b] for b in data))
    return out.tobytes()


def ulaw_to_pcm16(data: bytes) -> bytes:
    return _decode(data, _ULAW_DEC)


def alaw_to_pcm16(data: bytes) -> bytes:
    return _decode(data, _ALAW_DEC)


ENCODERS = {"PCMU": pcm16_to_ulaw, "PCMA": pcm16_to_alaw}
DECODERS = {"PCMU": ulaw_to_pcm16, "PCMA": alaw_to_pcm16}
SILENCE = {"PCMU": b"\xff", "PCMA": b"\xd5"}


def encode(codec: str, pcm16: bytes) -> bytes:
    return ENCODERS[codec](pcm16)


def decode(codec: str, data: bytes) -> bytes:
    return DECODERS[codec](data)


def rms_pcm16(pcm16: bytes) -> float:
    n = len(pcm16) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm16[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5
