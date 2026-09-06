"""Monitor: periodic PBX-side sampling (design §3.6 host metrics), log tail and
ESL call events. Everything here is PBX-generic; service-specific parsing is
left to plugins (stage 4)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from semishigure.pbx.adapter import PbxAdapter

log = logging.getLogger(__name__)

# FreeSWITCH: "... [WARNING] ..."   Asterisk: "[ts] WARNING[pid] file.c: ..."
LEVEL_RE = re.compile(r"\[(WARNING|ERR|CRIT|ALERT|NOTICE|INFO|DEBUG)\]|\b(WARNING|ERROR|NOTICE|VERBOSE|DEBUG)\[\d+\]")
_LEVEL_ALIAS = {"ERROR": "ERR"}


@dataclass
class MonitorCommand:
    name: str
    cmd: str
    kind: str = "shell"  # shell | api
    parse: str = "number"  # number | text


@dataclass
class MonitorState:
    interval: float = 2.0
    series: deque = field(default_factory=lambda: deque(maxlen=7200))
    last: dict = field(default_factory=dict)
    log_lines: deque = field(default_factory=lambda: deque(maxlen=400))
    log_level_counts: Counter = field(default_factory=Counter)
    log_lines_total: int = 0
    esl_events: Counter = field(default_factory=Counter)
    hangup_causes: Counter = field(default_factory=Counter)
    pbx_calls: dict = field(default_factory=dict)  # our call id -> {uuid, answered, cause}
    errors: deque = field(default_factory=lambda: deque(maxlen=50))
    samples_taken: int = 0


class Monitor:
    def __init__(self, adapter: PbxAdapter, interval: float = 2.0, commands: list[MonitorCommand] | None = None, correlation_var: str = "variable_sip_h_X-Semishigure-Call", on_line: Callable[[str], None] | None = None):
        self.adapter = adapter
        self.state = MonitorState(interval=interval)
        self.commands = list(commands or [])
        self.correlation_var = correlation_var
        self.on_line = on_line
        self.plugins: list = []  # objects with optional parse_log_line(line) / collect_metrics(adapter)
        self._task: asyncio.Task | None = None
        self._log_task: asyncio.Task | None = None
        self._prev_jiffies: tuple | None = None
        self.running = False

    async def start(self, tail_log: bool = True, esl_events: bool = True) -> None:
        self.running = True
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="monitor")
        if tail_log:
            self._log_task = asyncio.get_running_loop().create_task(self._tail_loop(), name="monitor-log")
        if esl_events:
            try:
                ok = await self.adapter.esl_subscribe(self._on_esl_event)  # type: ignore[attr-defined]
                if not ok:
                    self.state.errors.append("ESL events unavailable (no event socket connection)")
            except Exception as exc:  # noqa: BLE001
                self.state.errors.append(f"ESL subscribe failed: {exc}")

    async def stop(self) -> None:
        self.running = False
        for t in (self._task, self._log_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = self._log_task = None

    # -- sampling --------------------------------------------------------------------

    async def _loop(self) -> None:
        while self.running:
            t0 = time.monotonic()
            try:
                await self.sample()
            except Exception as exc:  # noqa: BLE001
                self.state.errors.append(f"{time.strftime('%H:%M:%S')} sample: {exc}")
                log.debug("monitor sample failed: %s", exc)
            await asyncio.sleep(max(0.2, self.state.interval - (time.monotonic() - t0)))

    async def sample(self) -> dict:
        point: dict = {"t": time.monotonic(), "wall": time.time()}
        try:
            point["channels"] = await self.adapter.channels_count()
        except Exception as exc:  # noqa: BLE001
            point["channels"] = None
            point["error"] = str(exc)
        try:
            pm = await self.adapter.process_metrics()
            point.update({"pid": pm.pid, "cpu_avg": pm.cpu_percent, "mem": pm.mem_percent, "nlwp": pm.threads, "rss_kb": pm.rss_kb})
            self._interval_cpu(pm, point)
        except Exception as exc:  # noqa: BLE001
            point["proc_error"] = str(exc)
        custom: dict = {}
        for c in self.commands:
            try:
                out = await self.adapter.api(c.cmd) if c.kind == "api" else await self.adapter.run_command(c.cmd)
                custom[c.name] = parse_output(out, c.parse)
            except Exception as exc:  # noqa: BLE001
                custom[c.name] = None
                self.state.errors.append(f"{c.name}: {exc}")
        for plugin in self.plugins:
            collect = getattr(plugin, "collect_metrics", None)
            if collect:
                try:
                    custom.update(await collect(self.adapter))
                except Exception as exc:  # noqa: BLE001
                    self.state.errors.append(f"plugin {plugin.__class__.__name__}: {exc}")
        point["custom"] = custom
        self.state.series.append(point)
        self.state.last = point
        self.state.samples_taken += 1
        return point

    def _interval_cpu(self, pm, point: dict) -> None:
        """%CPU over the last interval from /proc jiffies (per process and per thread),
        which shows saturation that the lifetime average from ps hides."""
        now = point["t"]
        prev = self._prev_jiffies
        self._prev_jiffies = (now, pm.proc_jiffies, dict(pm.thread_jiffies), pm.clk_tck)
        cpu = pm.cpu_percent
        top: list[dict] = []
        by_name: dict[str, dict] = {}
        if prev is not None and pm.proc_jiffies is not None and prev[1] is not None:
            dt = max(now - prev[0], 1e-3)
            cpu = round((pm.proc_jiffies - prev[1]) / dt / pm.clk_tck * 100, 1)
            for tid, (name, j) in pm.thread_jiffies.items():
                pj = prev[2].get(tid)
                tcpu = round((j - pj[1]) / dt / pm.clk_tck * 100, 1) if pj else 0.0
                top.append({"tid": tid, "cpu": tcpu, "name": name})
                agg = by_name.setdefault(name, {"count": 0, "cpu": 0.0})
                agg["count"] += 1
                agg["cpu"] = round(agg["cpu"] + tcpu, 1)
            top.sort(key=lambda t: t["cpu"], reverse=True)
        else:
            top = pm.top_threads
            by_name = pm.threads_by_name
        point["cpu"] = cpu
        point["top_threads"] = top[:10]
        point["threads_by_name"] = by_name

    # -- log tail ----------------------------------------------------------------------

    async def _tail_loop(self) -> None:
        while self.running:
            gen = self.adapter.log_stream()
            try:
                async for line in gen:
                    self._on_log_line(line)
                    if not self.running:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.state.errors.append(f"log tail: {exc}")
                await asyncio.sleep(3)
            finally:
                # close the generator now (kills tail -F) instead of at interpreter exit
                try:
                    await gen.aclose()
                except Exception:  # noqa: BLE001
                    pass

    def _on_log_line(self, line: str) -> None:
        self.state.log_lines_total += 1
        self.state.log_lines.append(line)
        m = LEVEL_RE.search(line)
        if m:
            level = m.group(1) or m.group(2) or ""
            self.state.log_level_counts[_LEVEL_ALIAS.get(level, level)] += 1
        if self.on_line:
            self.on_line(line)
        for plugin in self.plugins:
            parse = getattr(plugin, "parse_log_line", None)
            if parse:
                try:
                    parse(line)
                except Exception:  # noqa: BLE001
                    pass

    # -- ESL events -----------------------------------------------------------------------

    def _on_esl_event(self, ev: dict) -> None:
        name = ev.get("Event-Name", "?")
        self.state.esl_events[name] += 1
        call_id = ev.get(self.correlation_var)
        direction = ev.get("Call-Direction", "")
        if call_id and direction == "inbound":  # the A-leg (our caller) carries our header
            rec = self.state.pbx_calls.setdefault(call_id, {"uuid": ev.get("Unique-ID"), "created": None, "answered": None, "hangup": None, "cause": None})
            if name == "CHANNEL_CREATE":
                rec["created"] = time.time()
            elif name == "CHANNEL_ANSWER":
                rec["answered"] = time.time()
            elif name == "CHANNEL_HANGUP_COMPLETE":
                rec["hangup"] = time.time()
                rec["cause"] = ev.get("Hangup-Cause")
                rec["billsec"] = ev.get("variable_billsec")
        if name == "CHANNEL_HANGUP_COMPLETE":
            self.state.hangup_causes[ev.get("Hangup-Cause", "?")] += 1
            if len(self.state.pbx_calls) > 5000:
                for k in list(self.state.pbx_calls)[:1000]:
                    self.state.pbx_calls.pop(k, None)

    # -- reading ---------------------------------------------------------------------------

    def snapshot(self, series_tail: int = 600, log_tail: int = 60) -> dict:
        s = self.state
        last = dict(s.last)
        return {
            "interval": s.interval,
            "samples": s.samples_taken,
            "last": {k: v for k, v in last.items() if k not in ("top_threads", "threads_by_name")},
            "top_threads": last.get("top_threads", []),
            "threads_by_name": last.get("threads_by_name", {}),
            "series": [{k: v for k, v in p.items() if k not in ("top_threads", "threads_by_name")} for p in list(s.series)[-series_tail:]],
            "log_tail": list(s.log_lines)[-log_tail:],
            "log_level_counts": dict(s.log_level_counts),
            "log_lines_total": s.log_lines_total,
            "esl_events": dict(s.esl_events),
            "hangup_causes": dict(s.hangup_causes),
            "pbx_calls": len(s.pbx_calls),
            "errors": list(s.errors)[-10:],
            "adapter": self.adapter.describe() if hasattr(self.adapter, "describe") else {},
        }


def _to_number(v: str) -> float | int:
    return float(v) if "." in v else int(v)


def parse_output(text: str, how: str = "number"):
    """number: first number; last: last number; regex:<pattern>: first group; text: raw."""
    text = text or ""
    if how == "text":
        return text.strip()[-2000:]
    if how.startswith("regex:"):
        m = re.search(how[6:], text, re.MULTILINE)
        if not m:
            return None
        v = m.group(1) if m.groups() else m.group(0)
        try:
            return _to_number(v)
        except ValueError:
            return v
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    if not nums:
        return None
    return _to_number(nums[-1] if how == "last" else nums[0])


def commands_from_scenario(monitor_cfg: dict) -> tuple[float, list[MonitorCommand]]:
    from semishigure.scenario.model import parse_duration

    interval = parse_duration(monitor_cfg.get("interval"), 2.0)
    cmds = []
    for c in monitor_cfg.get("commands") or []:
        cmds.append(MonitorCommand(name=str(c.get("name")), cmd=str(c.get("cmd") or c.get("api") or ""), kind="api" if c.get("api") else "shell", parse=str(c.get("parse", "number"))))
    return interval, [c for c in cmds if c.cmd]
