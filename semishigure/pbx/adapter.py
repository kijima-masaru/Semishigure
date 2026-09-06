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


def make_adapter(profile: PbxProfile, executor: Executor, secrets: SecretStore | None = None) -> PbxAdapter:
    if profile.type == "freeswitch":
        return FreeSwitchAdapter(profile, executor, secrets)
    raise ValueError(f"PBX type {profile.type!r} is not supported yet (stage 5: asterisk)")
