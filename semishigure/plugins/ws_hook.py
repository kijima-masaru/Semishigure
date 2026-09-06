"""Per-call WebSocket hook: connect to a service when a call is established, optionally
wait for a message, send a JSON message, keep the connection until the call ends.

Config::

    ws_hook:
      url: "wss://example/staging?chatuuid={chat_uuid}"
      variables:                     # PBX channel variables fetched with uuid_getvar on the A-leg
        chat_uuid: chat_uuid
      wait_for: { type: senderror, code: 9001 }   # JSON fields that must match before sending
      wait_timeout: 60s
      send: { type: openChat, "A-lang": "{header.X-LANG}", "B-lang": "ja", leg: "B" }
      send_delay: 0s
      headers: { Authorization: "Bearer {secret:chatapi_token}" }
      connect_timeout: 10s
      max_concurrent: 50

Placeholders: ``{call_id}``, ``{sip_call_id}``, ``{pbx_uuid}``, ``{var.NAME}``
(fetched variables), ``{header.NAME}`` (caller INVITE headers), ``{secret:NAME}``.
Counters: opened / sent / wait_timeout / failed / closed.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from semishigure.plugins.base import Plugin, PluginContext
from semishigure.scenario.model import parse_duration

_PLACEHOLDER = re.compile(r"\{(call_id|sip_call_id|pbx_uuid|var\.[\w.-]+|header\.[\w.-]+|secret:[\w.-]+)\}")


class WsHookPlugin(Plugin):
    """Opens a WebSocket per established call, waits for a trigger message and sends a JSON message."""

    name = "ws_hook"

    def __init__(self, config: dict, ctx: PluginContext):
        super().__init__(config, ctx)
        if not config.get("url"):
            raise ValueError("ws_hook: 'url' is required")
        self.url_t = str(config["url"])
        self.variables: dict[str, str] = dict(config.get("variables") or {})
        self.wait_for: dict | None = config.get("wait_for")
        self.wait_timeout = parse_duration(config.get("wait_timeout"), 60.0)
        self.send: Any = config.get("send")
        self.send_delay = parse_duration(config.get("send_delay"), 0.0)
        self.headers_t: dict[str, str] = dict(config.get("headers") or {})
        self.connect_timeout = parse_duration(config.get("connect_timeout"), 10.0)
        self.max_concurrent = int(config.get("max_concurrent", 50))
        self._tasks: dict[str, asyncio.Task] = {}
        self._sockets: dict[str, Any] = {}
        self.received: int = 0

    # -- placeholders ------------------------------------------------------------

    def _render(self, template: Any, values: dict[str, str]) -> Any:
        if isinstance(template, str):
            def sub(m: re.Match) -> str:
                key = m.group(1)
                if key.startswith("secret:"):
                    return self.ctx.secrets.resolve(key) if self.ctx.secrets else ""
                return str(values.get(key, ""))

            return _PLACEHOLDER.sub(sub, template)
        if isinstance(template, dict):
            return {k: self._render(v, values) for k, v in template.items()}
        if isinstance(template, list):
            return [self._render(v, values) for v in template]
        return template

    async def _values_for(self, call) -> dict[str, str]:
        values = {"call_id": call.id, "sip_call_id": call.sip_call_id}
        for name, value in getattr(call, "headers", None) or []:
            values[f"header.{name}"] = value
        pbx = self.ctx.pbx_call(call.id)
        uuid = pbx.get("uuid") if pbx else None
        if uuid is None and self.ctx.monitor is not None and self.variables:
            # the ESL/AMI event may lag a little behind SIP
            for _ in range(20):
                await asyncio.sleep(0.1)
                pbx = self.ctx.pbx_call(call.id)
                if pbx and pbx.get("uuid"):
                    uuid = pbx["uuid"]
                    break
        values["pbx_uuid"] = uuid or ""
        adapter = self.ctx.adapter
        if self.variables and adapter is not None and uuid and hasattr(adapter, "uuid_getvar"):
            for name, var in self.variables.items():
                for _ in range(10):  # the service may set the variable a moment after answer
                    v = await adapter.uuid_getvar(uuid, var)
                    if v:
                        values[f"var.{name}"] = v
                        break
                    await asyncio.sleep(0.5)
        return values

    # -- hooks ----------------------------------------------------------------------

    async def on_call_established(self, call) -> None:
        if len(self._tasks) >= self.max_concurrent:
            self.counters["skipped_max_concurrent"] += 1
            return
        task = asyncio.get_running_loop().create_task(self._session(call), name=f"ws-hook-{call.id}")
        self._tasks[call.id] = task
        task.add_done_callback(lambda _t, cid=call.id: self._tasks.pop(cid, None))

    async def on_call_ended(self, call) -> None:
        ws = self._sockets.pop(call.id, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.counters["closed"] += 1
        task = self._tasks.get(call.id)
        if task and not task.done():
            task.cancel()

    async def post_run(self) -> None:
        for cid, ws in list(self._sockets.items()):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._sockets.pop(cid, None)
        for t in list(self._tasks.values()):
            t.cancel()

    async def _session(self, call) -> None:
        try:
            import websockets
        except ImportError:
            self.fail("the 'websockets' package is required")
            return
        try:
            values = await self._values_for(call)
            url = self._render(self.url_t, values)
            headers = self._render(self.headers_t, values)
            missing = [k for k in self.variables if f"var.{k}" not in values]
            if missing:
                self.counters["missing_variable"] += 1
                self.fail(f"call {call.id}: variables not available: {', '.join(missing)}")
                return
            ws = await asyncio.wait_for(websockets.connect(url, additional_headers=headers or None), self.connect_timeout)
        except Exception as exc:  # noqa: BLE001
            self.counters["failed"] += 1
            self.fail(f"call {call.id}: connect failed: {exc}")
            return
        self._sockets[call.id] = ws
        self.counters["opened"] += 1
        try:
            if self.wait_for:
                ok = await self._wait(ws)
                if not ok:
                    self.counters["wait_timeout"] += 1
                    return
            if self.send is not None:
                if self.send_delay > 0:
                    await asyncio.sleep(self.send_delay)
                payload = self._render(self.send, values)
                await ws.send(json.dumps(payload) if not isinstance(payload, str) else payload)
                self.counters["sent"] += 1
            async for _msg in ws:  # keep the connection; discard what the service sends
                self.received += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.counters["failed"] += 1
            self.fail(f"call {call.id}: {exc}")
        finally:
            self._sockets.pop(call.id, None)

    async def _wait(self, ws) -> bool:
        deadline = asyncio.get_running_loop().time() + self.wait_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                msg = await asyncio.wait_for(ws.recv(), remaining)
            except TimeoutError:
                return False
            self.received += 1
            try:
                data = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and all(str(data.get(k)) == str(v) for k, v in self.wait_for.items()):
                return True

    def snapshot(self) -> dict:
        return {"counters": dict(self.counters), "active": len(self._sockets), "received": self.received, "errors": self.errors[-5:]}
