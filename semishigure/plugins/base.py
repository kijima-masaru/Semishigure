"""Plugin interface (design §3.5, generalised).

A plugin adds service- or site-specific behaviour on top of the generic
load test. The core never depends on a particular service: it calls the
hooks below and merges whatever metrics/rows a plugin returns.

All hooks are optional. Async hooks are awaited; ``parse_log_line`` is
synchronous because it runs for every log line.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from semishigure.core.call import CallRecord
    from semishigure.pbx.adapter import PbxAdapter

log = logging.getLogger(__name__)


@dataclass
class PluginContext:
    """What a plugin may use. ``adapter`` is None when the run has no PBX profile."""

    run_name: str
    scenario: Any
    adapter: PbxAdapter | None = None
    secrets: Any = None
    monitor: Any = None
    events: Counter = field(default_factory=Counter)

    def pbx_call(self, call_id: str) -> dict | None:
        """PBX-side info for one of our calls (uuid, answered, cause) when the monitor saw it."""
        if self.monitor is None:
            return None
        return self.monitor.state.pbx_calls.get(call_id)


class Plugin:
    name: str = "plugin"
    description: str = ""

    def __init__(self, config: dict, ctx: PluginContext):
        self.config = config or {}
        self.ctx = ctx
        self.errors: list[str] = []
        self.counters: Counter = Counter()

    # -- lifecycle --------------------------------------------------------------

    async def pre_run(self) -> None:
        """Before the first call. Raise to abort the run."""

    async def post_run(self) -> None:
        """After the last call, also on abnormal termination (SIGINT, exception)."""

    # -- per call -------------------------------------------------------------------

    async def on_call_established(self, call: CallRecord) -> None: ...

    async def on_call_ended(self, call: CallRecord) -> None: ...

    # -- periodic / streaming -----------------------------------------------------

    async def collect_metrics(self, adapter: PbxAdapter | None) -> dict[str, Any]:
        """Called every monitor interval. Returned keys are prefixed with the plugin name."""
        return {}

    def parse_log_line(self, line: str) -> None: ...

    # -- reporting ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Live state for the UI / summary (JSON serialisable)."""
        return {"counters": dict(self.counters), "errors": self.errors[-5:]}

    def report_rows(self) -> list[tuple[str, Any]]:
        """Rows for the record sheet: (label, value)."""
        return [(f"{self.name}.{k}", v) for k, v in sorted(self.counters.items())]

    def fail(self, message: str) -> None:
        self.errors.append(message)
        log.warning("plugin %s: %s", self.name, message)
