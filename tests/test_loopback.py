"""UAC <-> UAS directly over UDP on loopback, no PBX involved."""

import asyncio
from pathlib import Path

import pytest

from semishigure.core.call import CallState
from semishigure.media.engine import PythonMediaEngine
from semishigure.media.wav import synth_speech_like
from semishigure.sip.endpoint import SipEndpoint
from semishigure.sip.uac import OutboundCall
from semishigure.sip.uas import Answerer, AnswererExtension


@pytest.fixture
async def stack():
    media = PythonMediaEngine(port_start=21000, port_end=21099, bind_ip="127.0.0.1")
    uac_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1")
    uas_ep = SipEndpoint("127.0.0.1", 0, bind_ip="127.0.0.1")
    await uac_ep.start()
    await uas_ep.start()
    exts = [AnswererExtension("9001", "pw", max_calls=1, audio=synth_speech_like(2.0, seed=2))]
    answerer = Answerer(uas_ep, media, ("127.0.0.1", 1), "loop.test", exts)
    yield uac_ep, uas_ep, media, answerer, exts[0]
    uac_ep.close()
    uas_ep.close()
    media.close()


async def test_call_media_and_bye(stack, tmp_path: Path):
    uac_ep, uas_ep, media, answerer, ext = stack
    call = OutboundCall(
        uac_ep,
        media,
        pbx_addr=("127.0.0.1", uas_ep.local_port),
        domain="loop.test",
        auth_user="9100",
        password="x",
        from_user="9100",
        destination="9001",
        headers=[("X-LANG", "en")],
        audio=synth_speech_like(2.0, seed=1),
        record_rx=tmp_path / "rx.wav",
    )
    rec = await call.start()
    assert rec.state == CallState.ESTABLISHED, rec.end_reason
    assert rec.codec == "PCMU"
    await asyncio.sleep(0.5)
    inbound = ext.calls[next(iter(ext.calls))]
    assert inbound.record.state == CallState.ESTABLISHED
    await call.hangup()
    await asyncio.sleep(0.2)
    assert rec.state == CallState.DONE and rec.end_reason == "local_bye" and rec.final_status == 200
    assert inbound.record.end_reason == "remote_bye"
    assert rec.media.tx_packets >= 20 and rec.media.rx_packets >= 15
    assert inbound.record.media.rx_packets >= 15
    assert rec.media.rx_level_rms > 500  # audio actually arrived
    assert rec.media.tx_late_max < 0.05
    assert (tmp_path / "rx.wav").stat().st_size > 1000


async def test_busy_when_max_calls_reached(stack):
    uac_ep, uas_ep, media, answerer, ext = stack
    first = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="9001", audio=None)
    await first.start()
    second = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9101", destination="9001", audio=None)
    rec2 = await second.start()
    assert rec2.state == CallState.DONE and rec2.final_status == 486
    assert ext.rejected_busy == 1
    await first.hangup()


async def test_cancel_before_answer(stack):
    uac_ep, uas_ep, media, answerer, ext = stack
    ext.answer_after = 5.0
    call = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="9001", audio=None)
    task = asyncio.create_task(call.start())
    await asyncio.sleep(0.3)
    assert call.record.state == CallState.RINGING
    await call.hangup()
    rec = await task
    assert rec.state == CallState.DONE and rec.end_reason == "cancelled" and rec.final_status == 487
    await asyncio.sleep(0.1)
    inbound = next(iter(answerer.records))
    assert inbound.end_reason == "cancelled"


async def test_remote_bye_from_answerer(stack):
    uac_ep, uas_ep, media, answerer, ext = stack
    call = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="9001", audio=None)
    await call.start()
    inbound = ext.calls[next(iter(ext.calls))]
    await inbound.hangup()
    await asyncio.sleep(0.1)
    assert call.record.end_reason == "remote_bye"
    assert inbound.record.end_reason == "local_bye" and inbound.record.final_status == 200


async def test_unknown_extension_404(stack):
    uac_ep, uas_ep, media, answerer, ext = stack
    call = OutboundCall(uac_ep, media, pbx_addr=("127.0.0.1", uas_ep.local_port), domain="loop.test", auth_user="a", password="b", from_user="9100", destination="7777", audio=None)
    rec = await call.start()
    assert rec.final_status == 404
