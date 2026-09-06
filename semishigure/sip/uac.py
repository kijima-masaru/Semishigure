"""Outbound call (UAC): INVITE with digest auth, ACK, media, BYE/CANCEL."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from semishigure.core.call import CallRecord, CallRole, CallState
from semishigure.media.base import MediaEngine, MediaSession
from semishigure.media.wav import AudioSource
from semishigure.sip.auth import DigestClient, challenge_from_response
from semishigure.sip.dialog import Dialog
from semishigure.sip.endpoint import SipEndpoint
from semishigure.sip.message import SipMessage, new_call_id, new_tag
from semishigure.sip.sdp import build_answer, build_offer, choose_codec, parse_sdp
from semishigure.sip.transaction import ClientTransaction, ServerTransaction
from semishigure.sip.transport import Addr

log = logging.getLogger(__name__)


class OutboundCall:
    def __init__(
        self,
        endpoint: SipEndpoint,
        media_engine: MediaEngine,
        *,
        pbx_addr: Addr,
        domain: str,
        auth_user: str,
        password: str,
        from_user: str,
        destination: str,
        headers: list[tuple[str, str]] | None = None,
        codecs: list[str] | None = None,
        audio: AudioSource | None = None,
        record_rx: Path | None = None,
        from_display: str | None = None,
        record: CallRecord | None = None,
    ):
        self.endpoint = endpoint
        self.media_engine = media_engine
        self.pbx_addr = pbx_addr
        self.domain = domain
        self.auth = DigestClient(auth_user, password)
        self.from_user = from_user
        self.from_display = from_display
        self.destination = destination
        self.headers = list(headers or [])
        self.codecs = list(codecs or ["PCMU", "PCMA"])
        self.audio = audio
        self.record_rx = record_rx
        self.record = record or CallRecord(role=CallRole.CALLER, local_user=from_user, remote_user=destination)
        self.record.sip_call_id = new_call_id(endpoint.local_ip)
        self.local_tag = new_tag()
        self.cseq = 0
        self.dialog: Dialog | None = None
        self.media: MediaSession | None = None
        self.invite_txn: ClientTransaction | None = None
        self.last_ack: SipMessage | None = None
        self.ended = asyncio.Event()
        self._responses: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._cancel_requested = False
        self._answered_uri = f"sip:{destination}@{domain}"

    # -- request building --------------------------------------------------------

    def _from_header(self) -> str:
        disp = f'"{self.from_display}" ' if self.from_display else ""
        return f"{disp}<sip:{self.from_user}@{self.domain}>;tag={self.local_tag}"

    def _build_invite(self, auth_header: tuple[str, str] | None = None) -> SipMessage:
        self.cseq += 1
        req = SipMessage.request("INVITE", self._answered_uri)
        req.add("Via", self.endpoint.via())
        req.add("From", self._from_header())
        req.add("To", f"<sip:{self.destination}@{self.domain}>")
        req.add("Call-ID", self.record.sip_call_id)
        req.add("CSeq", f"{self.cseq} INVITE")
        req.add("Contact", f"<{self.endpoint.contact_uri(self.from_user)}>")
        req.add("Max-Forwards", "70")
        if auth_header:
            req.add(*auth_header)
        for name, value in self.headers:
            req.add(name, value)
        req.add("Content-Type", "application/sdp")
        assert self.media is not None
        req.body = build_offer(self.media.local_ip, self.media.local_port, self.codecs)
        return req

    # -- main flow ---------------------------------------------------------------

    async def start(self) -> CallRecord:
        """Send INVITE and return once the call is established or has failed."""
        rec = self.record
        self.media = self.media_engine.create_session(self.endpoint.local_ip, self.audio, self.record_rx)
        rec.media = self.media.stats()
        req = self._build_invite()
        rec.set_state(CallState.INVITING)
        rec.t_invite = rec.mark("invite_sent")
        self.invite_txn = self.endpoint.send_request(req, self.pbx_addr, on_response=self._responses.put_nowait)
        try:
            while True:
                resp = await self._responses.get()
                status = resp.status or 0
                if status < 200:
                    self._on_provisional(resp)
                    continue
                if status in (401, 407) and rec.auth_rounds < 2:
                    ch = challenge_from_response(status, resp.get)
                    if ch is None:
                        rec.finish("auth_challenge_unparsable", status)
                        break
                    challenge, header_name = ch
                    rec.auth_rounds += 1
                    rec.set_state(CallState.AUTH, f"auth_{status}")
                    auth_value = self.auth.authorization(challenge, "INVITE", self._answered_uri)
                    req = self._build_invite((header_name, auth_value))
                    self.invite_txn = self.endpoint.send_request(req, self.pbx_addr, on_response=self._responses.put_nowait)
                    rec.mark("invite_auth_sent")
                    continue
                if 200 <= status < 300:
                    if rec.state == CallState.ESTABLISHED:
                        self._send_ack()  # 2xx retransmission
                        continue
                    self._on_2xx(req, resp)
                    if self._cancel_requested:
                        await self.hangup("cancelled_after_answer")
                    break
                # final failure
                reason = "cancelled" if status == 487 and self._cancel_requested else f"sip_{status}"
                if resp.get("X-Semishigure-Local") == "timeout":
                    reason = "invite_timeout"
                self._end(reason, status)
                break
        except asyncio.CancelledError:
            raise
        return rec

    def _on_provisional(self, resp: SipMessage) -> None:
        rec = self.record
        status = resp.status or 0
        if status == 100 and rec.t_100 is None:
            rec.t_100 = rec.mark("rx_100")
        elif status == 180 and rec.t_180 is None:
            rec.t_180 = rec.mark("rx_180")
            rec.set_state(CallState.RINGING)
        elif status == 183:
            if rec.t_183 is None:
                rec.t_183 = rec.mark("rx_183")
            if rec.state != CallState.RINGING:
                rec.set_state(CallState.RINGING)
        if resp.body and "sdp" in resp.content_type and self.media is not None:
            media = parse_sdp(resp.body)
            if media:
                chosen = choose_codec(media, self.codecs)
                if chosen:
                    codec, pt = chosen
                    self.media.set_remote(media.ip, media.port, codec, pt)  # early media: receive only

    def _on_2xx(self, invite: SipMessage, resp: SipMessage) -> None:
        rec = self.record
        rec.t_200 = rec.mark("rx_200")
        rec.final_status = resp.status
        self.dialog = Dialog.from_uac(invite, resp, self.endpoint.contact_uri(self.from_user))
        self.endpoint.register_dialog(self.dialog, self)
        self._send_ack()
        rec.t_ack = rec.mark("ack_sent")
        media = parse_sdp(resp.body) if resp.body else None
        if media is None:
            log.warning("2xx without SDP; ending call %s", rec.id)
            asyncio.get_running_loop().create_task(self.hangup("no_sdp"))
            return
        chosen = choose_codec(media, self.codecs)
        if chosen is None or self.media is None:
            asyncio.get_running_loop().create_task(self.hangup("codec_mismatch"))
            return
        codec, pt = chosen
        rec.codec = codec
        self.media.set_remote(media.ip, media.port, codec, pt)
        rec.t_established = rec.mark("established")
        rec.set_state(CallState.ESTABLISHED)
        self.media.start()

    def _send_ack(self) -> None:
        assert self.dialog is not None
        ack = self.dialog.create_request("ACK", self.endpoint.local_ip, self.endpoint.local_port, cseq=self.cseq)
        self.last_ack = ack
        self.endpoint.send_ack(ack, self.dialog.next_hop())

    # -- termination ---------------------------------------------------------------

    async def hangup(self, reason: str = "local_bye") -> None:
        rec = self.record
        if rec.state == CallState.DONE:
            return
        if rec.state == CallState.ESTABLISHED and self.dialog is not None:
            rec.set_state(CallState.TERMINATING)
            bye = self.dialog.create_request("BYE", self.endpoint.local_ip, self.endpoint.local_port)
            rec.t_bye = rec.mark("bye_sent")
            txn = self.endpoint.send_request(bye, self.dialog.next_hop())
            try:
                await txn.wait_final(timeout=6.0)
            except TimeoutError:
                log.warning("no response to BYE for call %s", rec.id)
            self._end(reason, txn.final.status if txn.final else None)
            return
        if rec.state in (CallState.INVITING, CallState.AUTH, CallState.RINGING) and self.invite_txn is not None:
            self._cancel_requested = True
            if rec.state == CallState.INVITING and not self.invite_txn.provisionals:
                # RFC 3261 §9.1: wait for a provisional before CANCEL; short grace period
                await asyncio.sleep(0.2)
            inv = self.invite_txn.request
            cancel = SipMessage.request("CANCEL", inv.uri or "")
            cancel.add("Via", inv.get("Via") or "")
            cancel.add("From", inv.get("From") or "")
            cancel.add("To", inv.get("To") or "")
            cancel.add("Call-ID", inv.call_id)
            cancel.add("CSeq", f"{inv.cseq[0]} CANCEL")
            cancel.add("Max-Forwards", "70")
            rec.mark("cancel_sent")
            self.endpoint.send_request(cancel, self.pbx_addr)
            try:
                await asyncio.wait_for(self.ended.wait(), timeout=8.0)
            except TimeoutError:
                self._end("cancel_timeout")
            return
        self._end(reason)

    def _end(self, reason: str, status: int | None = None) -> None:
        rec = self.record
        if self.media is not None:
            self.media.stop()
        if self.dialog is not None:
            self.endpoint.unregister_dialog(self.dialog)
        rec.finish(reason, status)
        self.ended.set()

    # -- DialogOwner -------------------------------------------------------------

    def on_in_dialog_request(self, request: SipMessage, txn: ServerTransaction) -> None:
        method = request.method
        rec = self.record
        if method == "BYE":
            txn.respond(SipMessage.response(200))
            rec.t_bye = rec.mark("bye_received")
            self._end("remote_bye")
        elif method == "INVITE":
            resp = SipMessage.response(200)
            resp.add("Contact", f"<{self.endpoint.contact_uri(self.from_user)}>")
            resp.add("Content-Type", "application/sdp")
            if request.body and self.media is not None:
                offer = parse_sdp(request.body)
                if offer and rec.codec:
                    self.media.set_remote(offer.ip, offer.port, rec.codec, offer.payload_types[0] if offer.payload_types else 0)
            if self.media is not None and rec.codec:
                resp.body = build_answer(self.media.local_ip, self.media.local_port, rec.codec, 101)
            txn.respond(resp)
            rec.mark("reinvite_answered")
        else:
            txn.respond(SipMessage.response(200))

    def on_ack(self, ack: SipMessage) -> None:
        pass
