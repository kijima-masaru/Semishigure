import asyncio
import sys
from pathlib import Path

import pytest

from semishigure.pbx.adapter import FreeSwitchAdapter, make_adapter
from semishigure.pbx.esl import parse_plain_event
from semishigure.pbx.executor import CommandResult, Executor, LocalExecutor
from semishigure.pbx.monitor import Monitor, commands_from_scenario
from semishigure.pbx.profile import PbxProfile, ProfileStore, build_executor

STATUS = """UP 0 years, 0 days, 1 hour, 2 minutes, 3 seconds, 4 milliseconds, 5 microseconds
FreeSWITCH (Version 1.10.12-release git a88d069 2024-08-02 21:02:27Z 64bit) is ready
12 session(s) since startup
4 session(s) - peak 20, last 5min 4
0 session(s) per Sec out of max 30, peak 5, last 5min 0
100 session(s) max
min idle cpu 0.00/94.47
Current Stack Size/Max 240K/8192K
"""


class FakeExecutor(Executor):
    kind = "fake"

    def __init__(self):
        self.commands: list[str] = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def run(self, command, timeout=15.0):
        self.commands.append(command)
        if "pidof" in command:
            self.calls = getattr(self, "calls", 0) + 1
            j = 100 * self.calls  # +100 jiffies per call
            out = (
                "PID=4242\n 12.5  0.8   40 123456\n---\n4242 0.0 freeswitch\n4300 55.5 flatline-net\n4301 3.0 flatline-tts\n4302 2.0 flatline-tts\n"
                f"---\n100\n4242 (freeswitch) S 1 1 1 0 -1 4 0 0 0 0 {j} {j} 0 0 20 0 40 0 1 2 3\n---\n"
                f"4242 (freeswitch) S 1 1 1 0 -1 4 0 0 0 0 1 1 0 0 20 0 40 0 1 2 3\n"
                f"4300 (flatline-net) R 1 1 1 0 -1 4 0 0 0 0 {j} {j} 0 0 20 0 40 0 1 2 3\n"
                f"4301 (flatline tts) S 1 1 1 0 -1 4 0 0 0 0 5 5 0 0 20 0 40 0 1 2 3\n"
            )
            return CommandResult(command, 0, out, "", 1.0)
        if command.startswith("tail"):
            return CommandResult(command, 0, "l1\nl2\n", "", 1.0)
        if "-x 'status'" in command or '-x status' in command:
            return CommandResult(command, 0, STATUS, "", 1.0)
        if "show channels count" in command:
            return CommandResult(command, 0, "8 total.\n", "", 1.0)
        return CommandResult(command, 0, "", "", 1.0)

    async def stream(self, command):
        for ln in ("2026-09-06 00:00:00.000000 [WARNING] a", "2026-09-06 00:00:01.000000 [ERR] b", "x [INFO] c", "[2026-09-06 03:57:37.752] WARNING[25939] ccss.c: asterisk style", "[2026-09-06 03:57:37.807] ERROR[25939] file.c: asterisk error"):
            yield ln
        await asyncio.sleep(10)

    async def forward(self, host, port):
        return host, port


def test_status_parse_and_metrics_via_fs_cli_fallback():
    profile = PbxProfile(name="t", fs_cli="fs_cli")  # no esl password ref -> fs_cli path
    ex = FakeExecutor()
    ad = make_adapter(profile, ex)
    assert isinstance(ad, FreeSwitchAdapter)

    async def go():
        await ad.connect()
        st = await ad.status()
        assert (st.sessions, st.sessions_peak, st.sessions_max, st.sps_max, st.sps_peak) == (4, 20, 100, 30, 5)
        assert st.version.startswith("1.10.12")
        assert await ad.channels_count() == 8
        pm = await ad.process_metrics()
        assert (pm.pid, pm.cpu_percent, pm.threads, pm.rss_kb) == (4242, 12.5, 40, 123456)
        assert pm.top_threads[0]["name"] == "flatline-net"
        assert pm.threads_by_name["flatline-tts"] == {"count": 2, "cpu": 5.0}
        assert await ad.log_tail(2) == ["l1", "l2"]
        await ad.close()

    asyncio.run(go())
    assert any("fs_cli" in c and "-x status" in c for c in ex.commands)


async def test_monitor_samples_and_log_levels():
    ad = make_adapter(PbxProfile(name="t"), FakeExecutor())
    await ad.connect()
    mon = Monitor(ad, interval=0.2, commands=commands_from_scenario({"interval": "200ms", "commands": [{"name": "ch", "api": "show channels count"}]})[1])
    await mon.start(esl_events=False)
    await asyncio.sleep(0.7)
    await mon.stop()
    snap = mon.snapshot()
    assert snap["samples"] >= 2
    assert snap["last"]["channels"] == 8 and snap["last"]["cpu_avg"] == 12.5
    assert snap["last"]["custom"]["ch"] == 8
    assert snap["log_level_counts"] == {"WARNING": 2, "ERR": 2, "INFO": 1}
    # interval CPU from /proc: +200 jiffies per ~0.2 s sample at CLK_TCK 100 -> far above the ps average
    assert snap["last"]["cpu"] > 100
    assert snap["threads_by_name"]["flatline-net"]["cpu"] > 100
    assert snap["threads_by_name"]["flatline tts"]["cpu"] == 0.0
    assert snap["top_threads"][0]["name"] == "flatline-net"


def test_parse_output_kinds():
    from semishigure.pbx.adapter import _parse_proc_stat
    from semishigure.pbx.monitor import parse_output

    text = "Registrations:\nCall-ID: 12\nTotal items returned: 4\n"
    assert parse_output(text, "number") == 12
    assert parse_output(text, "last") == 4
    assert parse_output(text, "regex:Total items returned: (\\d+)") == 4
    assert parse_output("x", "regex:(\\d+)") is None
    assert parse_output(" 0.04 \n", "number") == 0.04
    assert parse_output("abc", "text") == "abc"
    assert _parse_proc_stat("4301 (flatline tts) S 1 1 1 0 -1 4 0 0 0 0 5 7 0 0 20 0 40 0 1 2 3") == (4301, "flatline tts", 12)


def test_esl_plain_event_parse():
    ev = parse_plain_event("Event-Name: CHANNEL_ANSWER\nUnique-ID: abc\nvariable_sip_h_X-Semishigure-Call: 0123abcd\nCaller-Caller-ID-Name: %E8%9D%89\n\n")
    assert ev["Event-Name"] == "CHANNEL_ANSWER"
    assert ev["variable_sip_h_X-Semishigure-Call"] == "0123abcd"
    assert ev["Caller-Caller-ID-Name"] == "蝉"


def test_monitor_esl_event_correlation():
    ad = make_adapter(PbxProfile(name="t"), FakeExecutor())
    mon = Monitor(ad)
    mon._on_esl_event({"Event-Name": "CHANNEL_CREATE", "Call-Direction": "inbound", "Unique-ID": "u1", "variable_sip_h_X-Semishigure-Call": "c1"})
    mon._on_esl_event({"Event-Name": "CHANNEL_ANSWER", "Call-Direction": "inbound", "Unique-ID": "u1", "variable_sip_h_X-Semishigure-Call": "c1"})
    mon._on_esl_event({"Event-Name": "CHANNEL_HANGUP_COMPLETE", "Call-Direction": "inbound", "Unique-ID": "u1", "variable_sip_h_X-Semishigure-Call": "c1", "Hangup-Cause": "NORMAL_CLEARING", "variable_billsec": "12"})
    mon._on_esl_event({"Event-Name": "CHANNEL_HANGUP_COMPLETE", "Call-Direction": "outbound", "Unique-ID": "u2", "Hangup-Cause": "LOSE_RACE"})
    assert mon.state.pbx_calls["c1"]["cause"] == "NORMAL_CLEARING" and mon.state.pbx_calls["c1"]["billsec"] == "12"
    assert mon.state.hangup_causes == {"NORMAL_CLEARING": 1, "LOSE_RACE": 1}
    assert mon.state.esl_events["CHANNEL_HANGUP_COMPLETE"] == 2


@pytest.mark.skipif(sys.platform == "win32", reason="shell commands")
async def test_local_executor_run_and_stream():
    ex = LocalExecutor()
    r = await ex.run("echo hello; echo err >&2; exit 3")
    assert (r.returncode, r.text, r.stderr.strip()) == (3, "hello", "err")
    lines = []
    async for ln in ex.stream("printf 'a\\nb\\n'"):
        lines.append(ln)
    assert lines == ["a", "b"]
    assert await ex.forward("127.0.0.1", 8021) == ("127.0.0.1", 8021)


def test_profile_store_roundtrip(tmp_path: Path):
    store = ProfileStore(tmp_path / "p.yaml")
    store.upsert(PbxProfile(name="a", host="10.0.0.1", executor="ssh", ssh_user="semi", ssh_key="~/.ssh/id", esl_password_ref="secret:esl", extra={"custom": 1}))
    store.upsert(PbxProfile(name="b"))
    assert [p.name for p in store.load_all()] == ["a", "b"]
    a = store.get("a")
    assert a is not None and a.ssh_user == "semi" and a.extra == {"custom": 1}
    assert "secret:esl" in (tmp_path / "p.yaml").read_text()
    ex = build_executor(a)
    assert ex.kind == "ssh" and ex.username == "semi"
    assert build_executor(store.get("b")).kind == "local"
    assert store.delete("a") and store.get("a") is None


class FakeAsteriskExecutor(FakeExecutor):
    async def run(self, command, timeout=15.0):
        self.commands.append(command)
        if "core show version" in command:
            return CommandResult(command, 0, "Asterisk 20.6.0~dfsg+~cs6.13.40431414-2build5 built by nobody\n", "", 1.0)
        if "core show channels count" in command:
            return CommandResult(command, 0, "24 active channels\n12 of 100 max active calls ( 12.00% of capacity)\n30 calls processed\n", "", 1.0)
        if "core show settings" in command:
            return CommandResult(command, 0, "  Maximum calls:               100 (Current 12)\n", "", 1.0)
        if "core show uptime" in command:
            return CommandResult(command, 0, "System uptime: 1 hour, 2 minutes\nLast reload: 1 hour\n", "", 1.0)
        if "pjsip show contacts" in command:
            return CommandResult(command, 0, " Contact:  9001/sip:9001@127.0.0.1:5080   abc  NonQual  nan\n Contact:  9002/sip:9002@127.0.0.1:5080   def  NonQual  nan\n\nObjects found: 2\n", "", 1.0)
        if "core show channels concise" in command:
            return CommandResult(command, 0, "PJSIP/9100-00000001!default!8001!1!Up!Dial!PJSIP/9001&PJSIP/9002!9100!!!3!12!(None)!123.1\n", "", 1.0)
        return await super().run(command, timeout)


def test_asterisk_adapter_parsing():
    profile = PbxProfile(name="a", type="asterisk", extra={"asterisk_conf": "/x/asterisk.conf"})
    ex = FakeAsteriskExecutor()
    ad = make_adapter(profile, ex)

    async def go():
        await ad.connect()
        st = await ad.status()
        assert st.version.startswith("20.6.0") and st.sessions == 12 and st.sessions_max == 100 and st.uptime.startswith("1 hour")
        assert await ad.channels_count() == 24
        assert await ad.registrations() == ["9001", "9002"]
        rows = await ad.channels()
        assert rows[0]["channel"] == "PJSIP/9100-00000001" and rows[0]["state"] == "Up"
        assert ad.profile.log_path == "/var/log/asterisk/full" and ad.profile.process_name == "asterisk"
        await ad.close()

    asyncio.run(go())
    assert any(c.startswith("asterisk -C /x/asterisk.conf -rx ") for c in ex.commands)


def test_asterisk_ami_event_translation():
    from semishigure.pbx.adapter import AsteriskAdapter

    ad = AsteriskAdapter(PbxProfile(name="a", type="asterisk"), FakeAsteriskExecutor())
    seen: list[dict] = []

    class FakeAmi:
        connected = True
        on_event = None

        async def events_on(self, mask):
            pass

    ad.ami = FakeAmi()  # type: ignore[assignment]
    asyncio.run(ad.esl_subscribe(seen.append))
    ad.ami.on_event({"Event": "Newchannel", "Channel": "PJSIP/9100-00000001", "Uniqueid": "u1", "ChanVariable(SEMI_CALL)": "c1"})
    ad.ami.on_event({"Event": "Newstate", "Channel": "PJSIP/9100-00000001", "Uniqueid": "u1", "ChannelStateDesc": "Up", "ChanVariable(SEMI_CALL)": "c1"})
    ad.ami.on_event({"Event": "Hangup", "Channel": "PJSIP/9002-00000003", "Uniqueid": "u3", "Cause-txt": "Answered elsewhere", "ChanVariable(SEMI_CALL)": "c1"})
    assert [e["Event-Name"] for e in seen] == ["CHANNEL_CREATE", "CHANNEL_ANSWER", "CHANNEL_HANGUP_COMPLETE"]
    assert seen[0]["Call-Direction"] == "inbound" and seen[2]["Call-Direction"] == "outbound"
    assert seen[0]["variable_sip_h_X-Semishigure-Call"] == "c1"
