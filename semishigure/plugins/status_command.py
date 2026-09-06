"""Run a status command periodically and extract several values from its output.

Config::

    status_command:
      api: "flatline_interpreter status"      # PBX api command (ESL / AMI) ...
      # cmd: "some shell command"             # ... or a shell command on the PBX host
      fields:                                  # regex with one group -> number (or text)
        - { name: ws_conns, regex: "ws conns[^0-9]*(\\\\d+)" }
        - { name: net_loop_max_ms, regex: "net loop max[^0-9]*(\\\\d+)" }
      keyvalue: true                           # also parse "key: value" / "key = value" lines
      first_line_as: version                   # store the first output line under this name
"""

from __future__ import annotations

import re

from semishigure.plugins.base import Plugin, PluginContext

_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _./-]{0,40}?)\s*[:=]\s*(-?\d+(?:\.\d+)?|\S+)\s*$")


class StatusCommandPlugin(Plugin):
    """Extracts fields from a periodic status command (api or shell) into the monitor series."""

    name = "status_command"

    def __init__(self, config: dict, ctx: PluginContext):
        super().__init__(config, ctx)
        self.api = config.get("api")
        self.cmd = config.get("cmd")
        if not self.api and not self.cmd:
            raise ValueError("status_command: 'api' or 'cmd' is required")
        self.fields = [(f["name"], re.compile(f["regex"], re.MULTILINE)) for f in config.get("fields") or []]
        self.keyvalue = bool(config.get("keyvalue", False))
        self.first_line_as = config.get("first_line_as")
        self.last_output = ""
        self.last: dict = {}
        self.max: dict = {}

    async def collect_metrics(self, adapter) -> dict:
        if adapter is None:
            return {}
        out = await adapter.api(self.api) if self.api else await adapter.run_command(self.cmd)
        self.last_output = out[-4000:]
        values: dict = {}
        if self.first_line_as and out.strip():
            values[self.first_line_as] = out.strip().splitlines()[0][:200]
        for name, rx in self.fields:
            m = rx.search(out)
            values[name] = _num(m.group(1)) if m else None
        if self.keyvalue:
            for ln in out.splitlines():
                m = _KV_RE.match(ln)
                if m:
                    key = re.sub(r"\W+", "_", m.group(1).strip().lower()).strip("_")
                    if key and key not in values:
                        values[key] = _num(m.group(2))
        for k, v in values.items():
            if isinstance(v, int | float):
                self.max[k] = v if k not in self.max else max(self.max[k], v)
        self.last = values
        self.counters["samples"] += 1
        return values

    def snapshot(self) -> dict:
        return {"last": self.last, "max": self.max, "samples": self.counters["samples"], "output": self.last_output[-1500:], "errors": self.errors[-5:]}

    def report_rows(self) -> list[tuple[str, object]]:
        return [(f"{self.name}.{k}（最終 / 最大）", f"{v} / {self.max.get(k, '-')}") for k, v in self.last.items()]


def _num(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v
