"""SipEndpoint: one UDP socket, transaction matching and dialog routing.

The endpoint is the single entry point the UAC/UAS layers use. It owns the
transport, matches responses to client transactions, absorbs request
retransmissions with server transactions, routes in-dialog requests to the
dialog owner and hands new out-of-dialog requests to a registered handler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from semishigure.sip.dialog import Dialog
from semishigure.sip.message import SipMessage, new_branch
from semishigure.sip.transaction import ClientTransaction, ServerTransaction
from semishigure.sip.transport import Addr, UdpTransport

log = logging.getLogger(__name__)

USER_AGENT = "Semishigure/0.1"
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, NOTIFY, INFO, UPDATE, REFER"


class DialogOwner(Protocol):
    """Implemented by UAC/UAS call objects that own a dialog."""

    def on_in_dialog_request(self, request: SipMessage, txn: ServerTransaction) -> None: ...

    def on_ack(self, ack: SipMessage) -> None: ...


RequestHandler = Callable[[SipMessage, ServerTransaction, Addr], None]


class SipEndpoint:
    def __init__(self, local_ip: str, local_port: int, bind_ip: str = "0.0.0.0", trace: bool = False):
        self.transport = UdpTransport(local_ip, local_port, bind_ip=bind_ip, trace=trace)
        self._client_txns: dict[tuple[str, str], ClientTransaction] = {}
        self._server_txns: dict[tuple[str, str], ServerTransaction] = {}
        self._invite_server_txns: dict[tuple[str, int], ServerTransaction] = {}  # (call-id, cseq) -> pending 2xx
        self._dialogs: dict[tuple[str, str, str], DialogOwner] = {}
        self.request_handler: RequestHandler | None = None
        self.transport.add_callback(self._on_message)

    @property
    def local_ip(self) -> str:
        return self.transport.local_ip

    @property
    def local_port(self) -> int:
        return self.transport.local_port

    async def start(self) -> None:
        await self.transport.start()

    def close(self) -> None:
        for t in list(self._client_txns.values()):
            t.cancel_timers()
        for s in list(self._server_txns.values()):
            s.terminate()
        self.transport.close()

    # -- helpers for building requests ------------------------------------------

    def contact_uri(self, user: str) -> str:
        return f"sip:{user}@{self.local_ip}:{self.local_port}"

    def via(self) -> str:
        return f"SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={new_branch()};rport"

    def decorate(self, req: SipMessage) -> None:
        if req.get("Max-Forwards") is None:
            req.add("Max-Forwards", "70")
        if req.get("User-Agent") is None:
            req.add("User-Agent", USER_AGENT)
        if req.method in ("INVITE", "REGISTER", "OPTIONS") and req.get("Allow") is None:
            req.add("Allow", ALLOW)

    # -- sending ------------------------------------------------------------------

    def send_request(self, req: SipMessage, addr: Addr, on_response: Callable[[SipMessage], None] | None = None) -> ClientTransaction:
        self.decorate(req)
        txn = ClientTransaction(self.transport, req, addr, on_response=on_response, on_terminate=self._client_terminated)
        self._client_txns[(txn.branch, txn.method)] = txn
        txn.start()
        return txn

    def send_ack(self, ack: SipMessage, addr: Addr) -> None:
        """ACK for 2xx is not part of any transaction."""
        self.decorate(ack)
        self.transport.send(ack, addr)

    def _client_terminated(self, txn: ClientTransaction) -> None:
        self._client_txns.pop((txn.branch, txn.method), None)

    def _server_terminated(self, txn: ServerTransaction) -> None:
        self._server_txns.pop(txn.key, None)
        if txn.is_invite:
            self._invite_server_txns.pop((txn.request.call_id, txn.request.cseq[0]), None)

    # -- dialogs -----------------------------------------------------------------

    def register_dialog(self, dialog: Dialog, owner: DialogOwner) -> None:
        self._dialogs[dialog.id] = owner

    def unregister_dialog(self, dialog: Dialog) -> None:
        self._dialogs.pop(dialog.id, None)

    def _find_dialog_owner(self, req: SipMessage) -> DialogOwner | None:
        # For a request we receive, our tag is in To and the peer's tag in From.
        to_tag = req.to_addr.tag or ""
        from_tag = req.from_addr.tag or ""
        return self._dialogs.get((req.call_id, to_tag, from_tag))

    # -- receiving ----------------------------------------------------------------

    def _on_message(self, msg: SipMessage, addr: Addr) -> None:
        if msg.is_response:
            self._on_response(msg, addr)
        else:
            self._on_request(msg, addr)

    def _on_response(self, resp: SipMessage, addr: Addr) -> None:
        branch = resp.branch or ""
        txn = self._client_txns.get((branch, resp.cseq_method))
        if txn is None:
            log.debug("response without transaction: %s branch=%s", resp.summary, branch[-8:])
            return
        txn.on_response(resp)

    def _on_request(self, req: SipMessage, addr: Addr) -> None:
        method = req.method or ""
        branch = req.branch or ""

        if method == "ACK":
            inv = self._server_txns.get((branch, "INVITE"))
            if inv is not None and inv.final is not None and (inv.final.status or 0) >= 300:
                inv.ack_received(req)
                return
            pending = self._invite_server_txns.get((req.call_id, req.cseq[0]))
            if pending is not None:
                pending.ack_received(req)
            owner = self._find_dialog_owner(req)
            if owner is not None:
                owner.on_ack(req)
            elif pending is None:
                log.debug("stray ACK for call-id %s", req.call_id)
            return

        if method == "CANCEL":
            inv = self._server_txns.get((branch, "INVITE"))
            cancel_txn = ServerTransaction(self.transport, req, addr, on_terminate=self._server_terminated)
            self._server_txns[cancel_txn.key] = cancel_txn
            if inv is None:
                cancel_txn.respond(SipMessage.response(481))
                return
            inv.cancel_received(req, cancel_txn)
            return

        existing = self._server_txns.get((branch, method))
        if existing is not None:
            existing.on_retransmission()
            return

        txn = ServerTransaction(self.transport, req, addr, on_terminate=self._server_terminated)
        self._server_txns[txn.key] = txn
        if txn.is_invite:
            self._invite_server_txns[(req.call_id, req.cseq[0])] = txn
            txn.respond(SipMessage.response(100))

        if req.to_addr.tag:
            owner = self._find_dialog_owner(req)
            if owner is None:
                txn.respond(SipMessage.response(481))
                return
            owner.on_in_dialog_request(req, txn)
            return

        if method == "OPTIONS":
            resp = SipMessage.response(200)
            resp.add("Allow", ALLOW)
            resp.add("Accept", "application/sdp")
            txn.respond(resp)
            return

        if self.request_handler is None:
            txn.respond(SipMessage.response(405 if method == "INVITE" else 200))
            return
        self.request_handler(req, txn, addr)

    async def wait_idle(self, timeout: float = 2.0) -> None:
        """Give pending transactions a moment to finish (used at shutdown)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not any(t.final is None for t in self._client_txns.values()):
                return
            await asyncio.sleep(0.05)
