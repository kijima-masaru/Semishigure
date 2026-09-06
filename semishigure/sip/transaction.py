"""SIP transaction layer (RFC 3261 §17) for UDP.

Client transactions retransmit requests and deliver responses; server
transactions retransmit final responses and absorb request retransmissions.
2xx handling for INVITE follows RFC 6026 (the transaction keeps an
"accepted" state so 2xx retransmissions reach the TU and the UAS keeps
retransmitting 2xx until ACK).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from semishigure.sip.message import SipMessage

if TYPE_CHECKING:
    from semishigure.sip.transport import Addr, UdpTransport

log = logging.getLogger(__name__)

T1 = 0.5
T2 = 4.0
T4 = 5.0
TIMER_B = 64 * T1
TIMER_D = 32.0
TIMER_H = 64 * T1


class ClientTransaction:
    """One outgoing request with retransmission and final-response wait."""

    def __init__(self, transport: UdpTransport, request: SipMessage, addr: Addr, on_response: Callable[[SipMessage], None] | None = None, on_terminate: Callable[[ClientTransaction], None] | None = None):
        self.transport = transport
        self.request = request
        self.addr = addr
        self.branch = request.branch or ""
        self.method = request.method or ""
        self.is_invite = self.method == "INVITE"
        self._callback = on_response
        self._on_terminate = on_terminate
        self.state = "calling"
        self.final: SipMessage | None = None
        self.final_event = asyncio.Event()
        self.provisionals: list[SipMessage] = []
        self.sent_at = 0.0
        self.retransmissions = 0
        self._timer: asyncio.TimerHandle | None = None
        self._timeout_handle: asyncio.TimerHandle | None = None
        self._interval = T1
        self._loop = asyncio.get_running_loop()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self.sent_at = time.monotonic()
        self.transport.send(self.request, self.addr)
        self._schedule_retransmit()
        self._timeout_handle = self._loop.call_later(TIMER_B, self._on_timeout)

    def _schedule_retransmit(self) -> None:
        self._timer = self._loop.call_later(self._interval, self._retransmit)

    def _retransmit(self) -> None:
        if self.state not in ("calling", "trying", "proceeding"):
            return
        if self.is_invite and self.state == "proceeding":
            return  # INVITE stops retransmitting once a provisional arrived
        self.transport.send(self.request, self.addr)
        self.retransmissions += 1
        self._interval = min(self._interval * 2, T2) if not self.is_invite else self._interval * 2
        self._schedule_retransmit()

    def _on_timeout(self) -> None:
        if self.final is not None:
            return
        log.warning("transaction timeout: %s branch=%s", self.request.summary, self.branch[-8:])
        timeout = SipMessage.response(408, "Request Timeout (local)")
        timeout.set("Call-ID", self.request.call_id)
        timeout.set("CSeq", self.request.get("CSeq") or "")
        timeout.set("From", self.request.get("From") or "")
        timeout.set("To", self.request.get("To") or "")
        timeout.add("X-Semishigure-Local", "timeout")
        self._deliver_final(timeout, local=True)

    def cancel_timers(self) -> None:
        for h in (self._timer, self._timeout_handle):
            if h:
                h.cancel()
        self._timer = self._timeout_handle = None

    def terminate(self) -> None:
        self.cancel_timers()
        self.state = "terminated"
        if self._on_terminate:
            self._on_terminate(self)

    # -- responses ------------------------------------------------------------

    def on_response(self, resp: SipMessage) -> None:
        status = resp.status or 0
        if status < 200:
            self.state = "proceeding"
            self.provisionals.append(resp)
            if self._callback:
                self._callback(resp)
            return
        if self.final is not None:
            # retransmitted final response
            if self.is_invite and 300 <= status:
                self._send_ack(resp)
            elif self.is_invite and 200 <= status < 300 and self._callback:
                self._callback(resp)  # RFC 6026: TU re-sends ACK
            return
        self._deliver_final(resp)

    def _deliver_final(self, resp: SipMessage, local: bool = False) -> None:
        self.cancel_timers()
        self.final = resp
        status = resp.status or 0
        if self.is_invite and 300 <= status and not local:
            self._send_ack(resp)
            self.state = "completed"
            self._loop.call_later(TIMER_D, self.terminate)
        elif self.is_invite and 200 <= status < 300:
            self.state = "accepted"
            self._loop.call_later(TIMER_B, self.terminate)
        else:
            self.state = "completed"
            self._loop.call_later(T4, self.terminate)
        if self._callback:
            self._callback(resp)
        self.final_event.set()

    def _send_ack(self, resp: SipMessage) -> None:
        """ACK for a non-2xx final response: same branch/CSeq number as the INVITE."""
        req = self.request
        ack = SipMessage.request("ACK", req.uri or "")
        ack.add("Via", req.get("Via") or "")
        ack.add("From", req.get("From") or "")
        ack.add("To", resp.get("To") or req.get("To") or "")
        ack.add("Call-ID", req.call_id)
        ack.add("CSeq", f"{req.cseq[0]} ACK")
        for r in req.get_all("Route"):
            ack.add("Route", r)
        ack.add("Max-Forwards", "70")
        ack.add("Content-Length", "0")
        self.transport.send(ack, self.addr)

    async def wait_final(self, timeout: float | None = None) -> SipMessage:
        await asyncio.wait_for(self.final_event.wait(), timeout)
        assert self.final is not None
        return self.final


class ServerTransaction:
    """One incoming request: sends responses and absorbs retransmissions."""

    def __init__(self, transport: UdpTransport, request: SipMessage, addr: Addr, on_terminate: Callable[[ServerTransaction], None] | None = None):
        self.transport = transport
        self.request = request
        self.addr = addr
        self.branch = request.branch or ""
        self.method = request.method or ""
        self.is_invite = self.method == "INVITE"
        self.last_response: SipMessage | None = None
        self.final: SipMessage | None = None
        self.acked = False
        self.cancelled = False
        self.state = "proceeding"
        self.on_ack: Callable[[SipMessage], None] | None = None
        self.on_cancel: Callable[[SipMessage], None] | None = None
        self.on_ack_timeout: Callable[[], None] | None = None
        self._on_terminate = on_terminate
        self._retrans_handle: asyncio.TimerHandle | None = None
        self._final_timeout_handle: asyncio.TimerHandle | None = None
        self._interval = T1
        self._loop = asyncio.get_running_loop()
        self.received_at = time.monotonic()

    @property
    def key(self) -> tuple[str, str]:
        return (self.branch, self.method)

    # -- responses ------------------------------------------------------------

    def respond(self, resp: SipMessage) -> None:
        """Send a response, filling in Via/From/To/Call-ID/CSeq from the request."""
        req = self.request
        if resp.get("Via") is None:
            for v in req.get_all("Via"):
                resp.add("Via", v)
        if resp.get("From") is None:
            resp.add("From", req.get("From") or "")
        if resp.get("To") is None:
            resp.add("To", req.get("To") or "")
        if resp.get("Call-ID") is None:
            resp.add("Call-ID", req.call_id)
        if resp.get("CSeq") is None:
            resp.add("CSeq", req.get("CSeq") or "")
        for rr in req.get_all("Record-Route"):
            if self.is_invite and resp.get("Record-Route") is None:
                resp.add("Record-Route", rr)
        # rport / received handling for the top Via
        via = resp.get("Via")
        if via and "rport" in via and "rport=" not in via:
            resp.set("Via", via.replace("rport", f"rport={self.addr[1]};received={self.addr[0]}", 1))
        self.last_response = resp
        status = resp.status or 0
        self.transport.send(resp, self.addr)
        if status >= 200 and self.final is None:
            self.final = resp
            if self.is_invite:
                # retransmit final until ACK (both 2xx per RFC 6026 and non-2xx)
                self.state = "accepted" if status < 300 else "completed"
                self._schedule_retransmit()
                self._final_timeout_handle = self._loop.call_later(TIMER_H, self._on_ack_timeout)
            else:
                self.state = "completed"
                self._loop.call_later(TIMER_H, self.terminate)

    def _schedule_retransmit(self) -> None:
        self._retrans_handle = self._loop.call_later(self._interval, self._retransmit_final)

    def _retransmit_final(self) -> None:
        if self.acked or self.final is None or self.state == "terminated":
            return
        self.transport.send(self.final, self.addr)
        self._interval = min(self._interval * 2, T2)
        self._schedule_retransmit()

    def _on_ack_timeout(self) -> None:
        if self.acked or self.state == "terminated":
            return
        log.warning("no ACK for %s final response (call-id %s)", self.method, self.request.call_id)
        if self.on_ack_timeout:
            self.on_ack_timeout()
        self.terminate()

    # -- incoming --------------------------------------------------------------

    def on_retransmission(self) -> None:
        if self.last_response is not None and not self.acked:
            self.transport.send(self.last_response, self.addr)

    def ack_received(self, ack: SipMessage) -> None:
        self.acked = True
        if self._retrans_handle:
            self._retrans_handle.cancel()
        if self._final_timeout_handle:
            self._final_timeout_handle.cancel()
        self.state = "confirmed"
        if self.on_ack:
            self.on_ack(ack)
        self._loop.call_later(T4, self.terminate)

    def cancel_received(self, cancel: SipMessage, cancel_txn: ServerTransaction) -> None:
        # 200 to the CANCEL itself, 487 to the INVITE if not yet answered.
        cancel_txn.respond(SipMessage.response(200))
        if self.final is None:
            self.cancelled = True
            self.respond(SipMessage.response(487))
            if self.on_cancel:
                self.on_cancel(cancel)

    def terminate(self) -> None:
        if self.state == "terminated":
            return
        for h in (self._retrans_handle, self._final_timeout_handle):
            if h:
                h.cancel()
        self.state = "terminated"
        if self._on_terminate:
            self._on_terminate(self)
