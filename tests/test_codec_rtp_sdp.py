import struct

from semishigure.media import codec
from semishigure.media.rtp import build_packet, parse_packet
from semishigure.media.wav import synth_speech_like
from semishigure.sip.sdp import build_offer, choose_codec, parse_sdp

REFERENCE_ULAW = {0: 0xFF, 1: 0xFF, -1: 0x7E, 8: 0xFE, 1000: 0xCE, -1000: 0x4E, 32767: 0x80, -32768: 0x00}  # values from CPython audioop
REFERENCE_ALAW = {0: 0xD5, 8: 0xD5, -8: 0x55, 1000: 0xFA, -1000: 0x7A, 32767: 0xAA, -32768: 0x2A}


def test_ulaw_reference_points():
    for sample, expected in REFERENCE_ULAW.items():
        assert codec.pcm16_to_ulaw(struct.pack("<h", sample))[0] == expected, sample


def test_alaw_reference_points():
    for sample, expected in REFERENCE_ALAW.items():
        assert codec.pcm16_to_alaw(struct.pack("<h", sample))[0] == expected, sample


def test_roundtrip_error_is_small():
    samples = list(range(-32768, 32768, 97))
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    for name in ("PCMU", "PCMA"):
        back = struct.unpack(f"<{len(samples)}h", codec.decode(name, codec.encode(name, pcm)))
        for a, b in zip(samples, back, strict=True):
            # G.711 quantisation error grows with magnitude; ~1/16 relative
            assert abs(a - b) <= max(16, abs(a) // 12), (name, a, b)


def test_rtp_roundtrip():
    pkt = build_packet(0, 65535, 0xFFFFFFF0, 0xDEADBEEF, b"\xff" * 160, marker=True)
    assert len(pkt) == 172
    p = parse_packet(pkt)
    assert p is not None
    assert (p.payload_type, p.seq, p.timestamp, p.ssrc, p.marker) == (0, 65535, 0xFFFFFFF0, 0xDEADBEEF, True)
    assert p.payload == b"\xff" * 160
    assert parse_packet(b"\x00" * 5) is None


def test_synth_is_frame_aligned_and_not_silent():
    src = synth_speech_like(2.0, seed=3)
    assert src.samples % 160 == 0
    assert len(src.payload("PCMU")) == src.samples
    assert codec.rms_pcm16(src.pcm16) > 1000


def test_sdp_offer_and_parse():
    offer = build_offer("10.0.0.2", 20000, ["PCMU", "PCMA"])
    m = parse_sdp(offer)
    assert m is not None
    assert (m.ip, m.port) == ("10.0.0.2", 20000)
    assert m.payload_types == [0, 8, 101]
    assert m.telephone_event_pt() == 101
    assert choose_codec(m, ["PCMA", "PCMU"]) == ("PCMA", 8)


def test_sdp_media_level_connection_wins():
    body = b"v=0\r\no=- 1 1 IN IP4 1.1.1.1\r\ns=-\r\nc=IN IP4 1.1.1.1\r\nt=0 0\r\nm=audio 4000 RTP/AVP 8 0\r\nc=IN IP4 2.2.2.2\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n"
    m = parse_sdp(body)
    assert m is not None and m.ip == "2.2.2.2" and m.port == 4000 and m.ptime == 20
    assert choose_codec(m, ["PCMU"]) == ("PCMU", 0)
