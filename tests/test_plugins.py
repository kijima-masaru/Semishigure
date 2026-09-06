import asyncio
import json
from pathlib import Path

import pytest

from semishigure.core.call import CallRecord, CallRole
from semishigure.pbx.executor import LocalExecutor
from semishigure.plugins.base import Plugin, PluginContext
from semishigure.plugins.conf_override import ConfOverridePlugin, apply_changes
from semishigure.plugins.log_patterns import LogPatternsPlugin, parse_timestamp
from semishigure.plugins.manager import PluginManager
from semishigure.plugins.registry import load_plugins, resolve_class
from semishigure.plugins.status_command import StatusCommandPlugin
from semishigure.plugins.ws_hook import WsHookPlugin


def ctx(adapter=None, monitor=None) -> PluginContext:
    return PluginContext(run_name="t", scenario=None, adapter=adapter, monitor=monitor)


class DummyPlugin(Plugin):
    """test plugin"""

    async def pre_run(self):
        self.counters["pre"] += 1

    async def on_call_established(self, call):
        self.counters["est"] += 1

    async def collect_metrics(self, adapter):
        return {"x": 1}


class BrokenPlugin(Plugin):
    async def on_call_established(self, call):
        raise RuntimeError("boom")

    async def collect_metrics(self, adapter):
        raise RuntimeError("boom2")


def test_registry_builtin_external_and_disabled():
    cfg = {"log_patterns": {"counters": []}, "mine": {"module": "tests.test_plugins:DummyPlugin", "k": 1}, "off": {"module": "tests.test_plugins:DummyPlugin", "enabled": False}, "unknown_thing": {}}
    plugins = load_plugins(cfg, ctx())
    assert [p.name for p in plugins] == ["log_patterns", "mine"]
    assert isinstance(plugins[1], DummyPlugin) and plugins[1].config == {"k": 1}
    with pytest.raises(ValueError):
        resolve_class("nocolon")
    with pytest.raises(TypeError):
        resolve_class("tests.test_plugins:ctx")


async def test_manager_isolates_plugin_errors():
    m = PluginManager({"good": {"module": "tests.test_plugins:DummyPlugin"}, "bad": {"module": "tests.test_plugins:BrokenPlugin"}}, ctx())
    await m.pre_run()
    rec = CallRecord(role=CallRole.CALLER, local_user="9100", remote_user="8001")
    m.call_established(rec)
    await asyncio.sleep(0.05)
    metrics = await m.collect_metrics(None)
    assert metrics == {"good.x": 1}
    snap = m.snapshot()
    assert snap["good"]["counters"] == {"pre": 1, "est": 1}
    assert any("boom" in e for e in snap["bad"]["errors"]) and any("boom2" in e for e in snap["bad"]["errors"])
    await m.post_run()
    await m.post_run()  # idempotent


LOG = """2026-09-05 10:00:00.100000 [INFO] main.lua START Session: aaaa-1
2026-09-05 10:00:01.000000 [INFO] 待機モード解除: A-Leg=en Session: aaaa-1
2026-09-05 10:00:04.500000 [INFO] 言語検出ロック: A-leg language=en Session: aaaa-1
2026-09-05 10:00:05.000000 [INFO] 再生開始 leg=B 受信から1.5s 後 後続=2件
2026-09-05 10:00:06.000000 [INFO] TTS受信完了 TTFB=0.42s 転送=1.0s 音声=3.0s レート=3.0x
2026-09-05 10:00:07.000000 [INFO] 再生開始 leg=B 受信から2.5s 後 後続=0件
2026-09-05 10:00:08.000000 [WARNING] http 429 too many requests
2026-09-05 10:00:09.000000 [INFO] 言語検出ロック: Session: zzzz-9
2026-09-05 10:00:10.000000 [INFO] main.lua END Session: aaaa-1
"""


def test_log_patterns_counters_timers_numbers():
    p = LogPatternsPlugin(
        {
            "key": r"Session: ([0-9a-z-]+)",
            "counters": [{"name": "start", "regex": r"main\.lua START"}, {"name": "end", "regex": r"main\.lua END"}, {"name": "http_429", "regex": r"\b429\b"}],
            "timers": [{"name": "detect_lock", "start": "待機モード解除", "end": "言語検出ロック"}, {"name": "call", "start": r"main\.lua START", "end": r"main\.lua END"}],
            "numbers": [{"name": "play_delay_s", "regex": r"受信から([0-9.]+)s 後"}, {"name": "ttfb_s", "regex": r"TTFB=([0-9.]+)s"}, {"name": "queued", "regex": r"後続=(\d+)件"}],
        },
        ctx(),
    )
    for ln in LOG.splitlines():
        p.parse_log_line(ln)
    snap = p.snapshot()
    assert snap["counters"] == {"start": 1, "end": 1, "http_429": 1}
    assert snap["timers"]["detect_lock"]["count"] == 1 and snap["timers"]["detect_lock"]["max"] == 3.5
    assert snap["timers"]["detect_lock"]["unmatched_end"] == 1  # zzzz-9 had no start
    assert snap["timers"]["call"]["mean"] == 9.9
    assert snap["numbers"]["play_delay_s"] == {"count": 2, "mean": 2.0, "max": 2.5, "min": 1.5, "last": 2.5}
    assert snap["numbers"]["ttfb_s"]["max"] == 0.42 and snap["numbers"]["queued"]["max"] == 2
    metrics = asyncio.run(p.collect_metrics(None))
    assert metrics["detect_lock.max_s"] == 3.5 and metrics["count.http_429"] == 1
    rows = dict(p.report_rows())
    assert rows["log_patterns.detect_lock 平均 / 最大 (s)"] == "3.5 / 3.5"
    assert parse_timestamp("[2026-09-06 03:57:37.752] WARNING[25939] ccss.c: x") == pytest.approx(parse_timestamp("2026-09-06 03:57:37.752000 [INFO] y"))


def test_conf_override_apply_changes_and_roundtrip(tmp_path: Path):
    xml = '<configuration>\n  <param name="langid-min-chars" value="4"/>\n  <param value="2" name="langid-confirm"/>\n  <param name="log-level" value="debug"/>\n</configuration>\nloglevel=debug\n'
    out = apply_changes(xml, {"langid-min-chars": 100000, "langid-confirm": 99}, [{"regex": r"^loglevel=.*$", "with": "loglevel=info"}])
    assert 'name="langid-min-chars" value="100000"' in out and 'value="99" name="langid-confirm"' in out and "loglevel=info" in out and 'value="debug"' in out

    conf = tmp_path / "x.conf.xml"
    conf.write_text(xml)
    marker = tmp_path / "reloaded"

    class FakeAdapter:
        executor = LocalExecutor()

    p = ConfOverridePlugin({"files": [{"path": str(conf), "params": {"langid-min-chars": 100000}}], "reload": f"touch {marker.as_posix()}"}, ctx(adapter=FakeAdapter()))

    async def go():
        await p.pre_run()
        assert 'value="100000"' in conf.read_text() and (tmp_path / "x.conf.xml.semishigure.bak").exists() and marker.exists()
        assert p.snapshot()["applied"] == [str(conf)]
        await p.post_run()

    asyncio.run(go())
    assert conf.read_text() == xml and not (tmp_path / "x.conf.xml.semishigure.bak").exists()
    assert p.snapshot()["restored"] == [str(conf)] and p.counters["restored"] == 1


def test_status_command_fields_and_keyvalue():
    class FakeAdapter:
        async def api(self, cmd):
            return "flatline_interpreter v1.4.4\nws conns: 3 (convert=2 readout=1)\nworkers langid=1 tts=4\nnet loop max: 57ms over100ms: 0\nlog level : info\n"

    p = StatusCommandPlugin({"api": "flatline_interpreter status", "first_line_as": "version", "fields": [{"name": "ws_conns", "regex": r"ws conns:\s*(\d+)"}, {"name": "net_loop_max_ms", "regex": r"net loop max:\s*(\d+)"}, {"name": "tts", "regex": r"tts=(\d+)"}], "keyvalue": True}, ctx())
    m = asyncio.run(p.collect_metrics(FakeAdapter()))
    assert m["version"].startswith("flatline_interpreter") and m["ws_conns"] == 3 and m["net_loop_max_ms"] == 57 and m["tts"] == 4
    assert m["log_level"] == "info"
    asyncio.run(p.collect_metrics(FakeAdapter()))
    assert p.snapshot()["max"]["ws_conns"] == 3 and p.counters["samples"] == 2


async def test_ws_hook_wait_and_send():
    websockets = pytest.importorskip("websockets")
    received: list[dict] = []
    connected = asyncio.Event()

    async def handler(ws):
        connected.set()
        await ws.send(json.dumps({"type": "senderror", "code": 1234}))  # not the trigger
        await ws.send(json.dumps({"type": "senderror", "code": 9001}))
        async for msg in ws:
            received.append(json.loads(msg))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        p = WsHookPlugin(
            {"url": f"ws://127.0.0.1:{port}/staging?chatuuid={{var.chat_uuid}}&c={{call_id}}", "variables": {"chat_uuid": "chat_uuid"}, "wait_for": {"type": "senderror", "code": 9001}, "wait_timeout": "5s", "send": {"type": "openChat", "A-lang": "{header.X-LANG}", "leg": "B"}},
            ctx(adapter=_UuidAdapter(), monitor=_FakeMonitor()),
        )
        rec = CallRecord(role=CallRole.CALLER, local_user="9100", remote_user="8001", id="call1", headers=[("X-LANG", "en")])
        await p.on_call_established(rec)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if received:
                break
        assert received == [{"type": "openChat", "A-lang": "en", "leg": "B"}]
        assert p.counters["opened"] == 1 and p.counters["sent"] == 1 and p.snapshot()["active"] == 1
        await p.on_call_ended(rec)
        await asyncio.sleep(0.1)
        assert p.counters["closed"] == 1 and p.snapshot()["active"] == 0


class _UuidAdapter:
    async def uuid_getvar(self, uuid, name):
        return "chat-xyz" if (uuid, name) == ("uuid-1", "chat_uuid") else ""


class _FakeMonitor:
    class state:  # noqa: N801
        pbx_calls = {"call1": {"uuid": "uuid-1"}}
