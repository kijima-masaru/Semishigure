"""Minimal Asterisk Manager Interface (AMI) client over plain TCP.

Login, run ``Command`` actions and receive events. Own implementation so no
extra dependency (and no GPL) is needed."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


class AmiError(RuntimeError):
    pass


class AmiClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 5038, username: str = "", secret: str = "", timeout: float = 5.0):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._reader_task: asyncio.Task | None = None
        self.connected = False
        self.on_event: Callable[[dict], None] | None = None
        self.events_received = 0
        self.banner = ""

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, TimeoutError) as exc:
            raise AmiError(f"ami connect {self.host}:{self.port}: {exc}") from exc
        self.banner = (await asyncio.wait_for(self._reader.readline(), self.timeout)).decode("utf-8", "replace").strip()
        self.connected = True
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop(), name="ami-reader")
        resp = await self.action("Login", Username=self.username, Secret=self.secret, Events="off")
        if resp.get("Response") != "Success":
            await self.close()
            raise AmiError(f"ami login failed: {resp.get('Message')}")

    async def close(self) -> None:
        self.connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if self._writer:
            try:
                self._writer.write(b"Action: Logoff\r\n\r\n")
                await asyncio.wait_for(self._writer.drain(), 1)
            except Exception:  # noqa: BLE001
                pass
            self._writer.close()
            self._writer = None

    async def action(self, name: str, **fields: str) -> dict:
        if self._writer is None:
            raise AmiError("not connected")
        action_id = str(next(self._ids))
        lines = [f"Action: {name}", f"ActionID: {action_id}"] + [f"{k}: {v}" for k, v in fields.items()]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[action_id] = fut
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, self.timeout * 4)
        finally:
            self._pending.pop(action_id, None)

    async def command(self, cli: str) -> str:
        resp = await self.action("Command", Command=cli)
        return resp.get("_output", "")

    async def events_on(self, mask: str = "call,system") -> None:
        await self.action("Events", EventMask=mask)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                msg = await self._read_message()
                if msg is None:
                    break
                if "ActionID" in msg and msg["ActionID"] in self._pending and ("Response" in msg):
                    fut = self._pending.get(msg["ActionID"])
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue
                if "Event" in msg:
                    self.events_received += 1
                    if self.on_event:
                        try:
                            self.on_event(msg)
                        except Exception:  # noqa: BLE001
                            log.exception("ami on_event failed")
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as exc:
            log.warning("ami reader stopped: %s", exc)
        finally:
            self.connected = False

    async def _read_message(self) -> dict | None:
        assert self._reader is not None
        msg: dict[str, str] = {}
        output: list[str] = []
        while True:
            line = await self._reader.readline()
            if not line:
                return None
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                if msg or output:
                    break
                continue
            if text == "--END COMMAND--":
                continue
            if ": " in text or text.endswith(":"):
                name, _, value = text.partition(":")
                if name in ("Output",):
                    output.append(value.strip())
                    continue
                if " " in name.strip():
                    output.append(text)  # command output line that happens to contain ': '
                    continue
                msg[name.strip()] = value.strip()
            else:
                output.append(text)
        if output:
            msg["_output"] = "\n".join(output)
        return msg
