"""UDP transport for SIP on top of asyncio.

One socket carries many calls. Received datagrams are parsed and handed to a
single callback; sending is fire-and-forget. Retransmission belongs to the
transaction layer, not here.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable

from semishigure.sip.message import SipMessage, SipParseError

log = logging.getLogger(__name__)

Addr = tuple[str, int]
RecvCallback = Callable[[SipMessage, Addr], None]


def discover_local_ip(peer_host: str, peer_port: int = 5060) -> str:
    """Find the local interface address that routes to *peer_host* (no packets sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_host, peer_port))
        return s.getsockname()[0]
    finally:
        s.close()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, transport: UdpTransport):
        self._t = transport

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        self._t._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("udp error: %s", exc)


class UdpTransport:
    def __init__(self, local_ip: str, local_port: int, bind_ip: str = "0.0.0.0", trace: bool = False):
        self.local_ip = local_ip  # advertised address (Via/Contact/SDP)
        self.local_port = local_port
        self.bind_ip = bind_ip
        self.trace = trace
        self._transport: asyncio.DatagramTransport | None = None
        self._callbacks: list[RecvCallback] = []
        self.rx_count = 0
        self.tx_count = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.setblocking(False)
        sock.bind((self.bind_ip, self.local_port))
        self.local_port = sock.getsockname()[1]
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        log.info("SIP UDP transport listening on %s:%d (advertised %s)", self.bind_ip, self.local_port, self.local_ip)

    def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    def add_callback(self, cb: RecvCallback) -> None:
        self._callbacks.append(cb)

    def send(self, msg: SipMessage, addr: Addr) -> None:
        if not self._transport:
            raise RuntimeError("transport not started")
        data = msg.to_bytes()
        if self.trace:
            log.debug("SIP >>> %s:%d\n%s", addr[0], addr[1], data.decode("utf-8", "replace"))
        self._transport.sendto(data, addr)
        self.tx_count += 1

    def _on_datagram(self, data: bytes, addr: Addr) -> None:
        self.rx_count += 1
        if len(data.strip(b"\r\n")) == 0:
            return  # keep-alive
        try:
            msg = SipMessage.parse(data)
        except SipParseError as exc:
            log.warning("dropping unparsable datagram from %s: %s", addr, exc)
            return
        if self.trace:
            log.debug("SIP <<< %s:%d\n%s", addr[0], addr[1], data.decode("utf-8", "replace"))
        for cb in self._callbacks:
            try:
                cb(msg, addr)
            except Exception:  # noqa: BLE001 - never let one bad message kill the receive loop
                log.exception("error while handling %s from %s", msg.summary, addr)
