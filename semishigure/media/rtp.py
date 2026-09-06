"""RTP packet build/parse (RFC 3550) for audio payloads."""

from __future__ import annotations

import struct
from dataclasses import dataclass

RTP_VERSION = 2
HEADER_LEN = 12
_HDR = struct.Struct("!BBHII")


def build_packet(payload_type: int, seq: int, timestamp: int, ssrc: int, payload: bytes, marker: bool = False) -> bytes:
    b0 = RTP_VERSION << 6
    b1 = (0x80 if marker else 0) | (payload_type & 0x7F)
    return _HDR.pack(b0, b1, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF) + payload


@dataclass(slots=True)
class RtpPacket:
    payload_type: int
    seq: int
    timestamp: int
    ssrc: int
    marker: bool
    payload: bytes


def parse_packet(data: bytes) -> RtpPacket | None:
    if len(data) < HEADER_LEN:
        return None
    b0, b1, seq, ts, ssrc = _HDR.unpack_from(data)
    if b0 >> 6 != RTP_VERSION:
        return None
    cc = b0 & 0x0F
    ext = b0 & 0x10
    pad = b0 & 0x20
    offset = HEADER_LEN + cc * 4
    if ext:
        if len(data) < offset + 4:
            return None
        ext_len = struct.unpack_from("!H", data, offset + 2)[0]
        offset += 4 + ext_len * 4
    end = len(data)
    if pad and end > offset:
        end -= data[-1]
    if offset > end:
        return None
    return RtpPacket(payload_type=b1 & 0x7F, seq=seq, timestamp=ts, ssrc=ssrc, marker=bool(b1 & 0x80), payload=data[offset:end])
