"""DTMF (RFC 2833) and call steps over the loopback UAC<->UAS stack."""

import asyncio
import socket

from semishigure.core.call import CallState
from semishigure.media.engine import PythonMediaEngine
from semishigure.media.rtp import parse_packet
from semishigure.media.wav import synth_speech_like
from semishigure.sip.endpoint import SipEndpoint
from semishigure.sip.uac import OutboundCall
from semishigure.sip.uas import Answerer, AnswererExtension


def test_dtmf_packets_rfc2833():
    engine = PythonMediaEngine(port_start=23000, port_end=23099, bind_ip="127.0.0.1")
    try:
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(2.0)
        s = engine.create_session("127.0.0.1", None)
        s.set_remote("127.0.0.1", sink.getsockname()[1], "PCMU", 0, dtmf_payload_type=101)
        secs = s.send_dtmf("1#", duration_ms=100, gap_ms=60)
        assert 0.4 < secs < 0.8
        pkts = []
        deadline = 3.0
        while deadline > 0:
            try:
                data, _ = sink.recvfrom(2048)
            except TimeoutError:
                break
            p = parse_packet(data)
            pkts.append(p)
            if len(pkts) >= 16:
                break
        events = [(p.payload[0], bool(p.payload[1] & 0x80), (p.payload[2] << 8) | p.payload[3], p.marker, p.timestamp) for p in pkts if p.payload_type == 101]
        digit1 = [e for e in events if e[0] == 1]
        pound = [e for e in events if e[0] == 11]
        assert len(digit1) == 8 and len(pound) == 8  # 5 progress packets + 3 end packets each
        assert digit1[0][3] is True and pound[0][3] is True  # marker on the first packet of each event
        assert all(e[4] == digit1[0][4] for e in digit1)  # timestamp fixed for the whole event
        assert [e[1] for e in digit1] == [False] * 5 + [True] * 3
        assert digit1[-1][2] == 800 and pound[-1][2] == 800  # duration 100 ms = 800 samples
        assert s.stats().extra["dtmf_events"] == 2
        s.stop()
        sink.close()
    finally:
        engine.close()


async def test_steps_hold_dtmf_bye_over_loopback():
    media = PythonMediaEngine(port_start=23100, port_end=23199, bind_ip="127.0.0.1")
    uac_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1")
    uas_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1")
    await uac_ep.start()
    await uas_ep.start()
    ext = AnswererExtension("9001", "pw", max_calls=1, audio=synth_speech_like(1.0, seed=2))
    Answerer(uas_ep, media, ("127.0.0.1", 1), "loop.test", [ext])
    try:
        call = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="9001", audio=synth_speech_like(1.0, seed=1))
        rec = await call.start()
        assert rec.state == CallState.ESTABLISHED
        t0 = asyncio.get_running_loop().time()
        await call.run_steps([{"hold": "0.3s"}, {"dtmf": "12"}, {"hold": "0.2s"}, "bye"], max_seconds=10)
        elapsed = asyncio.get_running_loop().time() - t0
        assert rec.state == CallState.DONE and rec.end_reason == "steps_bye"
        assert 0.8 < elapsed < 3.0
        events = [e for _, e in rec.timeline]
        assert "dtmf:12" in events
        assert rec.media.extra.get("dtmf_events") == 2
        await asyncio.sleep(0.1)
    finally:
        uac_ep.close()
        uas_ep.close()
        media.close()


async def test_steps_stop_when_far_end_hangs_up():
    media = PythonMediaEngine(port_start=23200, port_end=23299, bind_ip="127.0.0.1")
    uac_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1")
    uas_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1")
    await uac_ep.start()
    await uas_ep.start()
    ext = AnswererExtension("9001", "pw", max_calls=1, audio=None)
    Answerer(uas_ep, media, ("127.0.0.1", 1), "loop.test", [ext])
    try:
        call = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="9001", audio=None)
        await call.start()
        inbound = next(iter(ext.calls.values()))
        task = asyncio.create_task(call.run_steps([{"hold": "10s"}, "bye"]))
        await asyncio.sleep(0.2)
        await inbound.hangup()
        await asyncio.wait_for(task, 2.0)
        assert call.record.end_reason == "remote_bye"
    finally:
        uac_ep.close()
        uas_ep.close()
        media.close()
