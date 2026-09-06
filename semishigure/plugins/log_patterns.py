"""Count, time and measure things in the PBX log (generic collect_metrics.sh).

Config::

    log_patterns:
      key: "Session: ([0-9a-f-]+)"        # default correlation key for timers (first group)
      counters:                            # how many lines match
        - { name: start, regex: "main\\\\.lua START" }
        - { name: http_429, regex: "\\\\b429\\\\b" }
      timers:                              # seconds from a start line to an end line, same key
        - { name: detect_lock, start: "待機モード解除", end: "言語検出ロック" }
      numbers:                             # a number captured on matching lines: count/mean/max/min
        - { name: play_delay_s, regex: "受信から([0-9.]+)s 後" }
        - { name: ttfb_s, regex: "TTFB=([0-9.]+)s" }
      timestamp: "^(\\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d+)"   # optional; default FreeSWITCH/Asterisk formats
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from semishigure.plugins.base import Plugin, PluginContext

_TS_RES = [
    re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"),  # FreeSWITCH / Asterisk full
]


def parse_timestamp(line: str, custom: re.Pattern | None = None) -> float | None:
    if custom is not None:
        m = custom.search(line)
        if m:
            try:
                return datetime.fromisoformat(m.group(1)).timestamp()
            except ValueError:
                return None
        return None
    for rx in _TS_RES:
        m = rx.search(line)
        if m:
            date, hh, mm, ss, frac = m.groups()
            try:
                base = datetime.strptime(f"{date} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return None
            return base + (float(f"0.{frac}") if frac else 0.0)
    return None


@dataclass
class _Stat:
    count: int = 0
    total: float = 0.0
    max: float | None = None
    min: float | None = None
    last: float | None = None

    def add(self, v: float) -> None:
        self.count += 1
        self.total += v
        self.last = v
        self.max = v if self.max is None else max(self.max, v)
        self.min = v if self.min is None else min(self.min, v)

    def as_dict(self) -> dict:
        return {"count": self.count, "mean": round(self.total / self.count, 3) if self.count else None, "max": self.max, "min": self.min, "last": self.last}


@dataclass
class _Timer:
    name: str
    start: re.Pattern
    end: re.Pattern
    key: re.Pattern | None
    pending: dict[str, float] = field(default_factory=dict)
    stat: _Stat = field(default_factory=_Stat)
    unmatched_end: int = 0


class LogPatternsPlugin(Plugin):
    """Counts lines, measures start->end intervals per key, and aggregates numbers found in the log."""

    name = "log_patterns"

    def __init__(self, config: dict, ctx: PluginContext):
        super().__init__(config, ctx)
        default_key = re.compile(config["key"]) if config.get("key") else None
        self.ts_re = re.compile(config["timestamp"]) if config.get("timestamp") else None
        self.counter_res = [(c["name"], re.compile(c["regex"])) for c in config.get("counters") or []]
        self.timers = [
            _Timer(t["name"], re.compile(t["start"]), re.compile(t["end"]), re.compile(t["key"]) if t.get("key") else default_key)
            for t in config.get("timers") or []
        ]
        self.number_res = [(n["name"], re.compile(n["regex"])) for n in config.get("numbers") or []]
        self.numbers: dict[str, _Stat] = {n: _Stat() for n, _ in self.number_res}
        self.lines = 0

    def parse_log_line(self, line: str) -> None:
        self.lines += 1
        for name, rx in self.counter_res:
            if rx.search(line):
                self.counters[name] += 1
        ts = None
        for t in self.timers:
            if t.start.search(line):
                key = self._key(t, line)
                if key is not None:
                    ts = ts or parse_timestamp(line, self.ts_re) or time.time()
                    t.pending[key] = ts
            elif t.end.search(line):
                key = self._key(t, line)
                if key is not None and key in t.pending:
                    ts = ts or parse_timestamp(line, self.ts_re) or time.time()
                    t.stat.add(round(ts - t.pending.pop(key), 3))
                else:
                    t.unmatched_end += 1
        for name, rx in self.number_res:
            m = rx.search(line)
            if m:
                try:
                    self.numbers[name].add(float(m.group(1)))
                except (ValueError, IndexError):
                    pass

    @staticmethod
    def _key(t: _Timer, line: str) -> str | None:
        if t.key is None:
            return "_"
        m = t.key.search(line)
        return m.group(1) if m else None

    async def collect_metrics(self, adapter) -> dict:
        out: dict = {f"count.{k}": v for k, v in self.counters.items()}
        for t in self.timers:
            s = t.stat.as_dict()
            out[f"{t.name}.count"] = s["count"]
            out[f"{t.name}.mean_s"] = s["mean"]
            out[f"{t.name}.max_s"] = s["max"]
            out[f"{t.name}.pending"] = len(t.pending)
        for name, st in self.numbers.items():
            s = st.as_dict()
            out[f"{name}.count"] = s["count"]
            out[f"{name}.mean"] = s["mean"]
            out[f"{name}.max"] = s["max"]
        return out

    def snapshot(self) -> dict:
        return {
            "lines": self.lines,
            "counters": dict(self.counters),
            "timers": {t.name: {**t.stat.as_dict(), "pending": len(t.pending), "unmatched_end": t.unmatched_end} for t in self.timers},
            "numbers": {n: s.as_dict() for n, s in self.numbers.items()},
            "errors": self.errors[-5:],
        }

    def report_rows(self) -> list[tuple[str, object]]:
        rows: list[tuple[str, object]] = [(f"{self.name}.{k}", v) for k, v in sorted(self.counters.items())]
        for t in self.timers:
            s = t.stat.as_dict()
            rows.append((f"{self.name}.{t.name} 平均 / 最大 (s)", f"{s['mean']} / {s['max']}" if s["count"] else "-"))
        for n, st in self.numbers.items():
            s = st.as_dict()
            rows.append((f"{self.name}.{n} 件数 / 平均 / 最大", f"{s['count']} / {s['mean']} / {s['max']}" if s["count"] else "-"))
        return rows
