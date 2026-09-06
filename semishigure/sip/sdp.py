"""Minimal SDP (RFC 4566) offer/answer for audio-only sessions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Static payload types we handle.
CODECS = {
    "PCMU": 0,
    "PCMA": 8,
}
CODEC_BY_PT = {v: k for k, v in CODECS.items()}
TELEPHONE_EVENT_PT = 101


@dataclass
class SdpMedia:
    ip: str
    port: int
    payload_types: list[int] = field(default_factory=list)
    rtpmap: dict[int, str] = field(default_factory=dict)  # pt -> "PCMU/8000"
    ptime: int | None = None
    direction: str = "sendrecv"

    def codec_name(self, pt: int) -> str | None:
        if pt in self.rtpmap:
            return self.rtpmap[pt].split("/")[0].upper()
        return CODEC_BY_PT.get(pt)

    def telephone_event_pt(self) -> int | None:
        for pt, name in self.rtpmap.items():
            if name.lower().startswith("telephone-event"):
                return pt
        return None


def build_offer(local_ip: str, port: int, codecs: list[str], session_id: int | None = None, dtmf: bool = True, ptime: int = 20) -> bytes:
    sid = session_id or int(time.time())
    pts = [CODECS[c] for c in codecs]
    fmt = " ".join(str(p) for p in pts)
    if dtmf:
        fmt += f" {TELEPHONE_EVENT_PT}"
    lines = [
        "v=0",
        f"o=semishigure {sid} {sid} IN IP4 {local_ip}",
        "s=semishigure",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {fmt}",
    ]
    for c in codecs:
        lines.append(f"a=rtpmap:{CODECS[c]} {c}/8000")
    if dtmf:
        lines.append(f"a=rtpmap:{TELEPHONE_EVENT_PT} telephone-event/8000")
        lines.append(f"a=fmtp:{TELEPHONE_EVENT_PT} 0-16")
    lines.append(f"a=ptime:{ptime}")
    lines.append("a=sendrecv")
    return ("\r\n".join(lines) + "\r\n").encode()


def build_answer(local_ip: str, port: int, codec: str, dtmf_pt: int | None = None, ptime: int = 20) -> bytes:
    sid = int(time.time())
    pt = CODECS[codec]
    fmt = str(pt) + (f" {dtmf_pt}" if dtmf_pt else "")
    lines = [
        "v=0",
        f"o=semishigure {sid} {sid} IN IP4 {local_ip}",
        "s=semishigure",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {fmt}",
        f"a=rtpmap:{pt} {codec}/8000",
    ]
    if dtmf_pt:
        lines.append(f"a=rtpmap:{dtmf_pt} telephone-event/8000")
        lines.append(f"a=fmtp:{dtmf_pt} 0-16")
    lines.append(f"a=ptime:{ptime}")
    lines.append("a=sendrecv")
    return ("\r\n".join(lines) + "\r\n").encode()


def parse_sdp(body: bytes | str) -> SdpMedia | None:
    """Return the first audio media description, or None."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    session_ip: str | None = None
    media: SdpMedia | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        key, val = line[0], line[2:]
        if key == "c":
            parts = val.split()
            if len(parts) >= 3:
                ip = parts[2].split("/")[0]
                if media is None:
                    session_ip = ip
                else:
                    media.ip = ip
        elif key == "m":
            parts = val.split()
            if media is not None:
                break  # only the first audio section matters
            if parts[0] != "audio" or len(parts) < 4:
                if parts[0] != "audio":
                    continue
            try:
                port = int(parts[1])
            except ValueError:
                continue
            pts: list[int] = []
            for p in parts[3:]:
                try:
                    pts.append(int(p))
                except ValueError:
                    pass
            media = SdpMedia(ip=session_ip or "0.0.0.0", port=port, payload_types=pts)
        elif key == "a" and media is not None:
            if val.startswith("rtpmap:"):
                pt_s, _, enc = val[7:].partition(" ")
                try:
                    media.rtpmap[int(pt_s)] = enc.strip()
                except ValueError:
                    pass
            elif val.startswith("ptime:"):
                try:
                    media.ptime = int(val[6:])
                except ValueError:
                    pass
            elif val in ("sendrecv", "sendonly", "recvonly", "inactive"):
                media.direction = val
    if media is not None and media.ip == "0.0.0.0" and session_ip:
        media.ip = session_ip
    return media


def choose_codec(media: SdpMedia, preferences: list[str]) -> tuple[str, int] | None:
    """Pick the first of *our* preferences that the peer offered/answered."""
    offered: dict[str, int] = {}
    for pt in media.payload_types:
        name = media.codec_name(pt)
        if name and name not in offered:
            offered[name] = pt
    for pref in preferences:
        if pref in offered:
            return pref, offered[pref]
    return None
