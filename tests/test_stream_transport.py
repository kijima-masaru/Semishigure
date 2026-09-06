"""UAC <-> UAS over TCP and TLS (loopback, no PBX)."""

import asyncio
from pathlib import Path

import pytest

from semishigure.core.call import CallState
from semishigure.media.engine import PythonMediaEngine
from semishigure.media.wav import synth_speech_like
from semishigure.sip.endpoint import SipEndpoint
from semishigure.sip.message import SipMessage
from semishigure.sip.tls import client_context, ensure_self_signed, server_context
from semishigure.sip.transport import StreamTransport
from semishigure.sip.uac import OutboundCall
from semishigure.sip.uas import Answerer, AnswererExtension


def test_stream_framing_splits_and_joins_messages():
    t = StreamTransport("127.0.0.1", 0, scheme="tcp")
    got: list[SipMessage] = []
    t.add_callback(lambda m, a: got.append(m))

    class C:
        peer = ("1.2.3.4", 5)
        buffer = b""

    conn = C()
    m1 = b"OPTIONS sip:a SIP/2.0\r\nCall-ID: 1\r\nContent-Length: 3\r\n\r\nabc"
    m2 = b"\r\n\r\nBYE sip:b SIP/2.0\r\nCall-ID: 2\r\nl: 0\r\n\r\n"
    data = m1 + m2
    for i in range(0, len(data), 7):  # arbitrary chunking
        conn.buffer += data[i : i + 7]
        t._drain(conn)
    assert [m.method for m in got] == ["OPTIONS", "BYE"] and got[0].body == b"abc"


@pytest.mark.parametrize("scheme", ["tcp", "tls"])
async def test_call_over_stream_transport(scheme: str, tmp_path: Path):
    tls_client = tls_server = None
    if scheme == "tls":
        cert, key = ensure_self_signed(tmp_path, ["127.0.0.1"])
        tls_client = client_context(verify=False)
        tls_server = server_context(cert, key)
    media = PythonMediaEngine(port_start=24000, port_end=24099, bind_ip="127.0.0.1")
    uac_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1", scheme=scheme, tls_client=tls_client, tls_server=tls_server)
    uas_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1", scheme=scheme, tls_client=tls_client, tls_server=tls_server)
    await uac_ep.start()
    await uas_ep.start()
    ext = AnswererExtension("9001", "pw", max_calls=1, audio=synth_speech_like(1.0, seed=2))
    Answerer(uas_ep, media, ("127.0.0.1", 1), "loop.test", [ext])
    try:
        assert uac_ep.transport_name == scheme.upper()
        assert f";transport={scheme}" in uac_ep.contact_uri("9100")
        call = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="9001", audio=synth_speech_like(1.0, seed=1))
        rec = await call.start()
        assert rec.state == CallState.ESTABLISHED, rec.end_reason
        await asyncio.sleep(0.4)
        inbound = next(iter(ext.calls.values()))
        assert inbound.record.state == CallState.ESTABLISHED
        # the answerer hangs up: its BYE travels over a new connection to the caller's listener
        await inbound.hangup()
        await asyncio.sleep(0.3)
        assert rec.end_reason == "remote_bye" and rec.media.rx_packets > 5
        assert uac_ep.transport.describe()["opened"] >= 1
    finally:
        uac_ep.close()
        uas_ep.close()
        media.close()
