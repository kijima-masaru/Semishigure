"""Minimal FreeSWITCH event socket (ESL) client over plain TCP.

Inbound mode: connect, authenticate, run ``api`` commands and subscribe to
events in plain text format. No external library (design §5)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import unquote

log = logging.getLogger(__name__)


class EslError(RuntimeError):
    pass


class EslClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8021, password: str = "", timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._replies: asyncio.Queue[dict] = asyncio.Queue()
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=10000)
        self._reader_task: asyncio.Task | None = None
        self.connected = False
        self.on_event: Callable[[dict], None] | None = None
        self.events_received = 0
        self.events_dropped = 0

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, TimeoutError) as exc:
            raise EslError(f"esl connect {self.host}:{self.port}: {exc}") from exc
        first = await self._read_message()
        if first.get("Content-Type") != "auth/request":
            raise EslError(f"unexpected greeting: {first}")
        await self._send(f"auth {self.password}")
        reply = await self._read_message()
        if not reply.get("Reply-Text", "").startswith("+OK"):
            self._writer.close()
            raise EslError(f"esl auth failed: {reply.get('Reply-Text')}")
        self.connected = True
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop(), name="esl-reader")

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
                self._writer.write(b"exit\n\n")
                await asyncio.wait_for(self._writer.drain(), 1)
            except Exception:  # noqa: BLE001
                pass
            self._writer.close()
            self._writer = None

    # -- protocol --------------------------------------------------------------

    async def _send(self, line: str) -> None:
        assert self._writer is not None
        self._writer.write(line.encode() + b"\n\n")
        await self._writer.drain()

    async def _read_message(self) -> dict:
        assert self._reader is not None
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(self._reader.readline(), self.timeout * 6)
            if not line:
                raise EslError("esl connection closed")
            line = line.rstrip(b"\r\n")
            if not line:
                break
            name, _, value = line.decode("utf-8", "replace").partition(":")
            headers[name.strip()] = unquote(value.strip())
        length = int(headers.get("Content-Length", "0") or 0)
        body = b""
        if length:
            body = await self._reader.readexactly(length)
        headers["_body"] = body.decode("utf-8", "replace")
        return headers

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = await self._read_message()
                ctype = msg.get("Content-Type", "")
                if ctype in ("command/reply", "api/response"):
                    await self._replies.put(msg)
                elif ctype == "text/event-plain":
                    ev = parse_plain_event(msg["_body"])
                    self.events_received += 1
                    if self.on_event:
                        try:
                            self.on_event(ev)
                        except Exception:  # noqa: BLE001
                            log.exception("esl on_event failed")
                    else:
                        try:
                            self._events.put_nowait(ev)
                        except asyncio.QueueFull:
                            self.events_dropped += 1
                elif ctype == "text/disconnect-notice":
                    log.warning("esl disconnect notice")
                    self.connected = False
                    return
        except asyncio.CancelledError:
            raise
        except (EslError, OSError, asyncio.IncompleteReadError) as exc:
            log.warning("esl reader stopped: %s", exc)
            self.connected = False

    # -- commands ------------------------------------------------------------------

    async def api(self, command: str, timeout: float | None = None) -> str:
        if not self.connected:
            raise EslError("not connected")
        await self._send(f"api {command}")
        reply = await asyncio.wait_for(self._replies.get(), timeout or self.timeout)
        return reply.get("_body", "")

    async def subscribe(self, events: list[str] | None = None) -> None:
        await self._send("event plain " + (" ".join(events) if events else "ALL"))
        reply = await asyncio.wait_for(self._replies.get(), self.timeout)
        if not reply.get("Reply-Text", "").startswith("+OK"):
            raise EslError(f"event subscribe failed: {reply.get('Reply-Text')}")

    async def filter(self, header: str, value: str) -> None:
        await self._send(f"filter {header} {value}")
        await asyncio.wait_for(self._replies.get(), self.timeout)

    async def next_event(self, timeout: float | None = None) -> dict:
        return await asyncio.wait_for(self._events.get(), timeout)


def parse_plain_event(body: str) -> dict[str, str]:
    ev: dict[str, str] = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        name, _, value = line.partition(":")
        ev[name.strip()] = unquote(value.strip())
    return ev
