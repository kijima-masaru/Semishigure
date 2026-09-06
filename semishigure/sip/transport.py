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
    transport_name = "UDP"

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

    def describe(self) -> dict:
        return {"scheme": "udp"}


# ---------------------------------------------------------------------------
# Stream transports: TCP and TLS (RFC 3261 §18 framing by Content-Length)
# ---------------------------------------------------------------------------


class _Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: Addr):
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self.buffer = b""
        self.task: asyncio.Task | None = None


class StreamTransport(UdpTransport):
    """SIP over TCP or TLS. One listener (our Contact port) plus on-demand outbound
    connections keyed by peer address; responses go back on the connection the
    request arrived on. The SIP engine uses the same send()/callback API as UDP."""

    def __init__(self, local_ip: str, local_port: int, scheme: str = "tcp", bind_ip: str = "0.0.0.0", trace: bool = False, tls_client=None, tls_server=None):
        super().__init__(local_ip, local_port, bind_ip=bind_ip, trace=trace)
        self.scheme = scheme.lower()
        self.tls_client = tls_client  # ssl.SSLContext for outbound connections (TLS only)
        self.tls_server = tls_server  # ssl.SSLContext for the listener (TLS only)
        self._server: asyncio.AbstractServer | None = None
        self._conns: dict[Addr, _Connection] = {}
        self._connecting: dict[Addr, asyncio.Future] = {}
        self.connections_opened = 0
        self.connections_failed = 0

    @property
    def transport_name(self) -> str:
        return "TLS" if self.scheme == "tls" else "TCP"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_accept, self.bind_ip, self.local_port, ssl=self.tls_server if self.scheme == "tls" else None, reuse_address=True)
        self.local_port = self._server.sockets[0].getsockname()[1]
        self._transport = True  # type: ignore[assignment]  # marks "started" for send()
        log.info("SIP %s transport listening on %s:%d (advertised %s)", self.transport_name, self.bind_ip, self.local_port, self.local_ip)

    def close(self) -> None:
        self._transport = None
        for conn in list(self._conns.values()):
            self._drop(conn)
        if self._server is not None:
            self._server.close()
            self._server = None

    # -- connections ------------------------------------------------------------

    async def _on_accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        addr: Addr = (peer[0], peer[1])
        conn = _Connection(reader, writer, addr)
        self._conns[addr] = conn
        self.connections_opened += 1
        await self._read_loop(conn)

    async def _connect(self, addr: Addr) -> _Connection:
        if addr in self._conns:
            return self._conns[addr]
        fut = self._connecting.get(addr)
        if fut is not None:
            return await fut
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._connecting[addr] = fut
        try:
            kwargs = {}
            if self.scheme == "tls":
                kwargs = {"ssl": self.tls_client, "server_hostname": addr[0]}
            reader, writer = await asyncio.wait_for(asyncio.open_connection(addr[0], addr[1], **kwargs), 10)
            conn = _Connection(reader, writer, addr)
            self._conns[addr] = conn
            self.connections_opened += 1
            conn.task = loop.create_task(self._read_loop(conn), name=f"sip-{self.scheme}-{addr[0]}:{addr[1]}")
            fut.set_result(conn)
            return conn
        except Exception as exc:
            self.connections_failed += 1
            fut.set_exception(exc)
            raise
        finally:
            self._connecting.pop(addr, None)

    def _drop(self, conn: _Connection) -> None:
        self._conns.pop(conn.peer, None)
        try:
            conn.writer.close()
        except Exception:  # noqa: BLE001
            pass
        if conn.task and conn.task is not asyncio.current_task():
            conn.task.cancel()

    async def _read_loop(self, conn: _Connection) -> None:
        try:
            while True:
                data = await conn.reader.read(65536)
                if not data:
                    break
                conn.buffer += data
                self._drain(conn)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            self._drop(conn)

    def _drain(self, conn: _Connection) -> None:
        while True:
            buf = conn.buffer.lstrip(b"\r\n")
            if not buf:
                conn.buffer = b""
                return
            sep = buf.find(b"\r\n\r\n")
            if sep < 0:
                conn.buffer = buf
                return
            head = buf[:sep]
            length = 0
            for line in head.split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.strip().lower() in (b"content-length", b"l"):
                    try:
                        length = int(value.strip())
                    except ValueError:
                        length = 0
            total = sep + 4 + length
            if len(buf) < total:
                conn.buffer = buf
                return
            message, conn.buffer = buf[:total], buf[total:]
            self._on_datagram(message, conn.peer)

    # -- sending ---------------------------------------------------------------------

    def send(self, msg: SipMessage, addr: Addr) -> None:
        if not self._transport:
            raise RuntimeError("transport not started")
        data = msg.to_bytes()
        if self.trace:
            log.debug("SIP >>> %s %s:%d\n%s", self.transport_name, addr[0], addr[1], data.decode("utf-8", "replace"))
        self.tx_count += 1
        conn = self._conns.get(addr)
        if conn is not None:
            conn.writer.write(data)
            return
        asyncio.get_running_loop().create_task(self._send_via_new_connection(data, addr))

    async def _send_via_new_connection(self, data: bytes, addr: Addr) -> None:
        try:
            conn = await self._connect(addr)
            conn.writer.write(data)
            await conn.writer.drain()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s connect to %s:%d failed: %s", self.transport_name, addr[0], addr[1], exc)

    def describe(self) -> dict:
        return {"scheme": self.scheme, "connections": len(self._conns), "opened": self.connections_opened, "failed": self.connections_failed}


def make_transport(scheme: str, local_ip: str, local_port: int, bind_ip: str = "0.0.0.0", trace: bool = False, tls_client=None, tls_server=None) -> UdpTransport:
    scheme = (scheme or "udp").lower()
    if scheme == "udp":
        return UdpTransport(local_ip, local_port, bind_ip=bind_ip, trace=trace)
    if scheme in ("tcp", "tls"):
        return StreamTransport(local_ip, local_port, scheme=scheme, bind_ip=bind_ip, trace=trace, tls_client=tls_client, tls_server=tls_server)
    raise ValueError(f"unknown SIP transport {scheme!r} (udp | tcp | tls)")
