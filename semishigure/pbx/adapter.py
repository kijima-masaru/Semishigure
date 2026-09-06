"""PbxAdapter (design §3.1): PBX-type specific operations, all via an Executor.

FreeSWITCH: API commands go over the event socket (own ESL client, port
forwarded over SSH when needed); ``fs_cli`` is the fallback. OS metrics and
logs use plain shell commands."""

from __future__ import annotations

import logging
import re
import shlex
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from semishigure.pbx.ami import AmiClient, AmiError
from semishigure.pbx.esl import EslClient, EslError
from semishigure.pbx.executor import Executor, ExecutorError
from semishigure.pbx.profile import PbxProfile
from semishigure.secrets import SecretStore

log = logging.getLogger(__name__)


@dataclass
class PbxStatus:
    version: str = ""
    uptime: str = ""
    sessions: int | None = None
    sessions_peak: int | None = None
    sessions_max: int | None = None
    sps_max: int | None = None
    sps_peak: int | None = None
    raw: str = ""


@dataclass
class ProcessMetrics:
    pid: int | None = None
    cpu_percent: float | None = None  # lifetime average from ps (like monitor.sh)
    mem_percent: float | None = None
    threads: int | None = None
    rss_kb: int | None = None
    clk_tck: int = 100
    proc_jiffies: int | None = None  # utime+stime of the whole process (/proc/PID/stat)
    thread_jiffies: dict[int, tuple[str, int]] = field(default_factory=dict)  # tid -> (name, utime+stime)
    top_threads: list[dict] = field(default_factory=list)  # [{tid, cpu, name}] (ps lifetime %CPU)
    threads_by_name: dict[str, dict] = field(default_factory=dict)  # name -> {count, cpu}


class PbxAdapter(ABC):
    def __init__(self, profile: PbxProfile, executor: Executor, secrets: SecretStore | None = None):
        self.profile = profile
        self.executor = executor
        self.secrets = secrets or SecretStore()

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def status(self) -> PbxStatus: ...

    @abstractmethod
    async def channels_count(self) -> int: ...

    @abstractmethod
    async def api(self, command: str) -> str: ...

    async def process_metrics(self) -> ProcessMetrics:
        name = shlex.quote(self.profile.process_name)
        cmd = (
            f"PID=$(pidof {name} | cut -d' ' -f1); [ -n \"$PID\" ] || exit 3; echo PID=$PID; "
            f"ps -o pcpu=,pmem=,nlwp=,rss= -p $PID; echo ---; ps -L -o tid=,pcpu=,comm= -p $PID; "
            f"echo ---; getconf CLK_TCK; cat /proc/$PID/stat; echo ---; cat /proc/$PID/task/*/stat"
        )
        r = await self.executor.run(cmd, timeout=10)
        pm = ProcessMetrics()
        if not r.ok:
            return pm
        lines = r.stdout.splitlines()
        section = 0
        for ln in lines:
            ln = ln.strip()
            if ln.startswith("PID="):
                pm.pid = int(ln[4:] or 0)
            elif ln == "---":
                section += 1
            elif section == 2 and ln:
                if ln.isdigit():
                    pm.clk_tck = int(ln)
                else:
                    parsed = _parse_proc_stat(ln)
                    if parsed:
                        pm.proc_jiffies = parsed[2]
            elif section == 3 and ln:
                parsed = _parse_proc_stat(ln)
                if parsed:
                    tid, comm, jiffies = parsed
                    pm.thread_jiffies[tid] = (comm, jiffies)
            elif section == 0 and ln:
                parts = ln.split()
                if len(parts) >= 4:
                    pm.cpu_percent = float(parts[0])
                    pm.mem_percent = float(parts[1])
                    pm.threads = int(parts[2])
                    pm.rss_kb = int(parts[3])
            elif section == 1 and ln:
                parts = ln.split(None, 2)
                if len(parts) >= 3:
                    try:
                        tid, cpu, name = int(parts[0]), float(parts[1]), parts[2]
                    except ValueError:
                        continue
                    pm.top_threads.append({"tid": tid, "cpu": cpu, "name": name})
                    agg = pm.threads_by_name.setdefault(name, {"count": 0, "cpu": 0.0})
                    agg["count"] += 1
                    agg["cpu"] = round(agg["cpu"] + cpu, 1)
        pm.top_threads.sort(key=lambda t: t["cpu"], reverse=True)
        pm.top_threads = pm.top_threads[:15]
        return pm

    def log_stream(self, path: str | None = None) -> AsyncIterator[str]:
        p = shlex.quote(path or self.profile.log_path)
        return self.executor.stream(f"tail -n 0 -F {p}")

    async def log_tail(self, lines: int = 100, path: str | None = None) -> list[str]:
        p = shlex.quote(path or self.profile.log_path)
        r = await self.executor.run(f"tail -n {int(lines)} {p}", timeout=10)
        return r.stdout.splitlines()

    async def grep_count(self, pattern: str, path: str | None = None) -> int:
        p = shlex.quote(path or self.profile.log_path)
        r = await self.executor.run(f"grep -c -- {shlex.quote(pattern)} {p} || true", timeout=20)
        try:
            return int(r.text.splitlines()[-1]) if r.text else 0
        except ValueError:
            return 0

    async def run_command(self, command: str, timeout: float = 15.0) -> str:
        r = await self.executor.run(command, timeout=timeout)
        return r.stdout if r.ok else (r.stdout + r.stderr)


def _parse_proc_stat(line: str) -> tuple[int, str, int] | None:
    """/proc/<pid>/stat -> (pid, comm, utime+stime). comm may contain spaces."""
    try:
        pid_s, _, rest = line.partition(" (")
        comm, _, fields = rest.rpartition(") ")
        parts = fields.split()
        # fields after ')' start at index 3 of the stat layout: state(3) ... utime(14) stime(15)
        utime, stime = int(parts[11]), int(parts[12])
        return int(pid_s), comm, utime + stime
    except (ValueError, IndexError):
        return None


class FreeSwitchAdapter(PbxAdapter):
    def __init__(self, profile: PbxProfile, executor: Executor, secrets: SecretStore | None = None):
        super().__init__(profile, executor, secrets)
        self.esl: EslClient | None = None
        self.esl_error: str = ""
        self._esl_addr: tuple[str, int] | None = None

    async def connect(self) -> None:
        await self.executor.connect()
        password = ""
        if self.profile.esl_password_ref:
            try:
                password = self.secrets.resolve(self.profile.esl_password_ref)
            except Exception as exc:  # noqa: BLE001
                self.esl_error = str(exc)
        if password:
            try:
                host, port = await self.executor.forward(self.profile.esl_host, self.profile.esl_port)
                self._esl_addr = (host, port)
                client = EslClient(host, port, password)
                await client.connect()
                self.esl = client
                self.esl_error = ""
            except (EslError, ExecutorError, OSError) as exc:
                self.esl = None
                self.esl_error = str(exc)
                log.warning("ESL unavailable (%s); falling back to fs_cli", exc)

    async def close(self) -> None:
        if self.esl is not None:
            await self.esl.close()
            self.esl = None
        await self.executor.close()

    async def api(self, command: str) -> str:
        if self.esl is not None and self.esl.connected:
            try:
                return await self.esl.api(command)
            except (EslError, TimeoutError) as exc:
                self.esl_error = str(exc)
                log.warning("esl api failed (%s); using fs_cli", exc)
        cli = self.profile.fs_cli
        pw = ""
        if self.profile.esl_password_ref:
            try:
                pw = self.secrets.resolve(self.profile.esl_password_ref)
            except Exception:  # noqa: BLE001
                pw = ""
        auth = f" -H {shlex.quote(self.profile.esl_host)} -P {self.profile.esl_port}" + (f" -p {shlex.quote(pw)}" if pw else "")
        r = await self.executor.run(f"{cli}{auth} -x {shlex.quote(command)}", timeout=15)
        return r.stdout

    async def status(self) -> PbxStatus:
        raw = await self.api("status")
        st = PbxStatus(raw=raw)
        m = re.search(r"FreeSWITCH \(Version ([^)]+)\)", raw)
        if m:
            st.version = m.group(1)
        m = re.search(r"UP (.+?)\n", raw)
        if m:
            st.uptime = m.group(1).strip()
        m = re.search(r"(\d+) session\(s\) - peak (\d+)", raw)
        if m:
            st.sessions, st.sessions_peak = int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d+) session\(s\) per Sec out of max (\d+), peak (\d+)", raw)
        if m:
            st.sps_max, st.sps_peak = int(m.group(2)), int(m.group(3))
        m = re.search(r"(\d+) session\(s\) max", raw)
        if m:
            st.sessions_max = int(m.group(1))
        return st

    async def channels_count(self) -> int:
        out = await self.api("show channels count")
        m = re.search(r"(\d+) total", out)
        return int(m.group(1)) if m else 0

    async def channels(self) -> list[dict]:
        import json

        out = await self.api("show channels as json")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        return list(data.get("rows") or [])

    async def uuid_getvar(self, uuid: str, name: str) -> str:
        out = (await self.api(f"uuid_getvar {uuid} {name}")).strip()
        return "" if out.startswith("-ERR") or out == "_undef_" else out

    async def registrations(self, profile_name: str = "internal") -> list[str]:
        out = await self.api(f"sofia status profile {profile_name} reg")
        return re.findall(r"Contact:\s+\"?[^<]*<?sip:([^@]+)@", out)

    async def limits(self) -> dict:
        st = await self.status()
        return {"max_sessions": st.sessions_max, "sessions_per_second": st.sps_max}

    async def esl_subscribe(self, on_event, events: list[str] | None = None) -> bool:
        if self.esl is None or not self.esl.connected:
            return False
        self.esl.on_event = on_event
        await self.esl.subscribe(events or ["CHANNEL_CREATE", "CHANNEL_ANSWER", "CHANNEL_HANGUP_COMPLETE"])
        return True

    def describe(self) -> dict:
        return {"type": "freeswitch", "executor": self.executor.describe(), "esl": "connected" if self.esl and self.esl.connected else f"unavailable ({self.esl_error})" if self.esl_error else "not configured"}


class AsteriskAdapter(PbxAdapter):
    """Asterisk via ``asterisk -rx`` (CLI over the executor) with AMI for events.
    ``profile.fs_cli`` holds the asterisk binary/CLI prefix (default ``asterisk``),
    ``profile.esl_*`` are reused for AMI (host/port/password ref, user in ``extra['ami_user']``)."""

    def __init__(self, profile: PbxProfile, executor: Executor, secrets: SecretStore | None = None):
        super().__init__(profile, executor, secrets)
        self.ami: AmiClient | None = None
        self.ami_error: str = ""
        if self.profile.fs_cli in ("", "fs_cli"):
            self.profile.fs_cli = "asterisk"
        if self.profile.log_path == "/var/log/freeswitch/freeswitch.log":
            self.profile.log_path = "/var/log/asterisk/full"
        if self.profile.process_name == "freeswitch":
            self.profile.process_name = "asterisk"

    async def connect(self) -> None:
        await self.executor.connect()
        password = ""
        if self.profile.esl_password_ref:
            try:
                password = self.secrets.resolve(self.profile.esl_password_ref)
            except Exception as exc:  # noqa: BLE001
                self.ami_error = str(exc)
        user = str(self.profile.extra.get("ami_user") or "semishigure")
        if password:
            try:
                host, port = await self.executor.forward(self.profile.esl_host, self.profile.esl_port or 5038)
                client = AmiClient(host, port, user, password)
                await client.connect()
                self.ami = client
                self.ami_error = ""
            except (AmiError, ExecutorError, OSError) as exc:
                self.ami = None
                self.ami_error = str(exc)
                log.warning("AMI unavailable (%s); using asterisk -rx", exc)

    async def close(self) -> None:
        if self.ami is not None:
            await self.ami.close()
            self.ami = None
        await self.executor.close()

    async def api(self, command: str) -> str:
        if self.ami is not None and self.ami.connected:
            try:
                return await self.ami.command(command)
            except (AmiError, TimeoutError) as exc:
                self.ami_error = str(exc)
                log.warning("ami command failed (%s); using asterisk -rx", exc)
        conf = self.profile.extra.get("asterisk_conf")
        cli = self.profile.fs_cli + (f" -C {shlex.quote(str(conf))}" if conf else "")
        r = await self.executor.run(f"{cli} -rx {shlex.quote(command)}", timeout=15)
        return r.stdout

    async def status(self) -> PbxStatus:
        version = await self.api("core show version")
        st = PbxStatus(raw=version)
        m = re.search(r"Asterisk\s+(\S+)", version)
        if m:
            st.version = m.group(1)
        chans = await self.api("core show channels count")
        m = re.search(r"(\d+)(?: of (\d+) max)? active calls?", chans)
        if m:
            st.sessions = int(m.group(1))
            if m.group(2):
                st.sessions_max = int(m.group(2))
        settings = await self.api("core show settings")
        m = re.search(r"Maximum calls:\s+(\S+)", settings)
        if m and m.group(1).isdigit():
            st.sessions_max = int(m.group(1))
        m = re.search(r"System uptime:\s+([^\n]+)", await self.api("core show uptime"))
        if m:
            st.uptime = m.group(1).strip()
        return st

    async def channels_count(self) -> int:
        out = await self.api("core show channels count")
        m = re.search(r"(\d+) active channel", out)
        return int(m.group(1)) if m else 0

    async def channels(self) -> list[dict]:
        out = await self.api("core show channels concise")
        rows = []
        for ln in out.splitlines():
            parts = ln.split("!")
            if len(parts) >= 6 and "/" in parts[0]:
                rows.append({"channel": parts[0], "context": parts[1], "exten": parts[2], "prio": parts[3], "state": parts[4], "application": parts[5], "data": parts[6] if len(parts) > 6 else ""})
        return rows

    async def registrations(self) -> list[str]:
        out = await self.api("pjsip show contacts")
        return sorted({m.group(1) for m in re.finditer(r"Contact:\s+(\d+)/sip:", out)})

    async def limits(self) -> dict:
        st = await self.status()
        return {"max_sessions": st.sessions_max, "sessions_per_second": None}

    async def esl_subscribe(self, on_event, events: list[str] | None = None) -> bool:
        """Event feed (same hook name as FreeSWITCH so Monitor stays generic)."""
        if self.ami is None or not self.ami.connected:
            return False

        def translate(ev: dict) -> None:
            name = ev.get("Event", "")
            mapped = {"Newchannel": "CHANNEL_CREATE", "Hangup": "CHANNEL_HANGUP_COMPLETE", "Newstate": None}.get(name)
            if name == "Newstate" and ev.get("ChannelStateDesc") == "Up":
                mapped = "CHANNEL_ANSWER"
            if not mapped:
                return
            chan = ev.get("Channel", "")
            # PJSIP/9100-0000000a is our caller (A-leg); other endpoints are B-legs
            direction = "inbound" if chan.startswith("PJSIP/9100-") or ev.get("ChanVariable(SEMI_CALL)") and not chan.startswith(tuple(f"PJSIP/{u}-" for u in ("9001", "9002", "9003", "9004"))) else "outbound"
            out = {
                "Event-Name": mapped,
                "Unique-ID": ev.get("Uniqueid", ""),
                "Call-Direction": direction,
                "Hangup-Cause": ev.get("Cause-txt", ev.get("Cause", "")),
                "variable_billsec": "",
                "variable_sip_h_X-Semishigure-Call": ev.get("ChanVariable(SEMI_CALL)", ""),
            }
            on_event(out)

        self.ami.on_event = translate
        await self.ami.events_on("call")
        return True

    def describe(self) -> dict:
        return {"type": "asterisk", "executor": self.executor.describe(), "esl": "connected (AMI)" if self.ami and self.ami.connected else f"unavailable ({self.ami_error})" if self.ami_error else "not configured"}


def make_adapter(profile: PbxProfile, executor: Executor, secrets: SecretStore | None = None) -> PbxAdapter:
    if profile.type == "freeswitch":
        return FreeSwitchAdapter(profile, executor, secrets)
    if profile.type == "asterisk":
        return AsteriskAdapter(profile, executor, secrets)
    raise ValueError(f"unknown PBX type {profile.type!r} (freeswitch | asterisk)")
