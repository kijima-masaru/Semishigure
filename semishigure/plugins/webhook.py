"""HTTP webhook on run/call events (JSON POST, stdlib only).

Config::

    webhook:
      url: https://example/hook
      events: [pre_run, post_run, call_established, call_ended]   # default: all
      headers: { Authorization: "Bearer {secret:hook_token}" }
      timeout: 5s
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

from semishigure.plugins.base import Plugin, PluginContext
from semishigure.scenario.model import parse_duration

ALL_EVENTS = ("pre_run", "post_run", "call_established", "call_ended")


class WebhookPlugin(Plugin):
    """POSTs a JSON document to a URL on run and call events."""

    name = "webhook"

    def __init__(self, config: dict, ctx: PluginContext):
        super().__init__(config, ctx)
        if not config.get("url"):
            raise ValueError("webhook: 'url' is required")
        self.url = str(config["url"])
        self.events = set(config.get("events") or ALL_EVENTS)
        self.headers = {k: self._secret(str(v)) for k, v in (config.get("headers") or {}).items()}
        self.timeout = parse_duration(config.get("timeout"), 5.0)

    def _secret(self, v: str) -> str:
        if "{secret:" in v and self.ctx.secrets is not None:
            import re

            return re.sub(r"\{secret:([\w.-]+)\}", lambda m: self.ctx.secrets.resolve(f"secret:{m.group(1)}"), v)
        return v

    async def _post(self, event: str, data: dict) -> None:
        if event not in self.events:
            return
        body = json.dumps({"event": event, "run": self.ctx.run_name, "time": time.time(), **data}).encode()

        def do() -> int:
            req = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/json", **self.headers})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status

        try:
            status = await asyncio.get_running_loop().run_in_executor(None, do)
            self.counters[f"{event}_ok"] += 1
            if status >= 300:
                self.fail(f"{event}: HTTP {status}")
        except Exception as exc:  # noqa: BLE001
            self.counters[f"{event}_failed"] += 1
            self.fail(f"{event}: {exc}")

    async def pre_run(self) -> None:
        await self._post("pre_run", {"scenario": getattr(self.ctx.scenario, "name", "")})

    async def post_run(self) -> None:
        await self._post("post_run", {})

    async def on_call_established(self, call) -> None:
        await self._post("call_established", {"call": call.summary()})

    async def on_call_ended(self, call) -> None:
        await self._post("call_ended", {"call": call.summary()})
