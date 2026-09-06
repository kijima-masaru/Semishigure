"""Answerer side (UAS): REGISTER with refresh, auto-answer INVITE, play WAV."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from semishigure.core.call import CallRecord, CallRole, CallState
from semishigure.media.base import MediaEngine, MediaSession
from semishigure.media.wav import AudioSource
from semishigure.sip.auth import DigestClient, challenge_from_response
from semishigure.sip.dialog import Dialog
from semishigure.sip.endpoint import SipEndpoint
from semishigure.sip.message import NameAddr, SipMessage, SipUri, new_call_id, new_tag
from semishigure.sip.sdp import build_answer, choose_codec, parse_sdp
from semishigure.sip.transaction import ServerTransaction
from semishigure.sip.transport import Addr

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class Registration:
    def __init__(self, endpoint: SipEndpoint, registrar: Addr, domain: str, user: str, password: str, expires: int = 300, on_state: Callable[[Registration], None] | None = None):
        self.endpoint = endpoint
        self.registrar = registrar
        self.domain = domain
        self.user = user
        self.auth = DigestClient(user, password)
        self.requested_expires = expires
        self.granted_expires = expires
        self.on_state = on_state
        self.state = "unregistered"
        self.last_status: int | None = None
        self.error: str = ""
        self.call_id = new_call_id(endpoint.local_ip)
        self.from_tag = new_tag()
        self.cseq = 0
        self.registered_at: float | None = None
        self._task: asyncio.Task | None = None
        self.register_rtt_ms: float | None = None

    @property
    def aor(self) -> str:
        return f"sip:{self.user}@{self.domain}"

    @property
    def contact(self) -> str:
        return self.endpoint.contact_uri(self.user)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            log.info("registration %s: %s", self.user, state)
            if self.on_state:
                self.on_state(self)

    def _build(self, expires: int, auth_header: tuple[str, str] | None = None) -> SipMessage:
        self.cseq += 1
        req = SipMessage.request("REGISTER", f"sip:{self.domain}")
        req.add("Via", self.endpoint.via())
        req.add("From", f"<{self.aor}>;tag={self.from_tag}")
        req.add("To", f"<{self.aor}>")
        req.add("Call-ID", self.call_id)
        req.add("CSeq", f"{self.cseq} REGISTER")
        req.add("Contact", f"<{self.contact}>")
        req.add("Expires", str(expires))
        if auth_header:
            req.add(*auth_header)
        return req

    async def _register_once(self, expires: int) -> bool:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        req = self._build(expires)
        txn = self.endpoint.send_request(req, self.registrar)
        resp = await txn.wait_final()
        status = resp.status or 0
        rounds = 0
        while status in (401, 407) and rounds < 2:
            rounds += 1
            ch = challenge_from_response(status, resp.get)
            if ch is None:
                break
            challenge, header_name = ch
            auth_value = self.auth.authorization(challenge, "REGISTER", f"sip:{self.domain}")
            req = self._build(expires, (header_name, auth_value))
            txn = self.endpoint.send_request(req, self.registrar)
            resp = await txn.wait_final()
            status = resp.status or 0
        if status == 423:
            min_exp = resp.get("Min-Expires")
            if min_exp and min_exp.isdigit() and expires > 0:
                self.requested_expires = int(min_exp)
                return await self._register_once(self.requested_expires)
        self.last_status = status
        self.register_rtt_ms = round((loop.time() - t0) * 1000, 1)
        if 200 <= status < 300:
            if expires > 0:
                self.granted_expires = self._granted_expires(resp, expires)
                self.registered_at = loop.time()
                self._set_state("registered")
            else:
                self._set_state("unregistered")
            return True
        self.error = f"{status} {resp.reason}"
        self._set_state("failed")
        return False

    def _granted_expires(self, resp: SipMessage, requested: int) -> int:
        mine = SipUri.parse(self.contact)
        for c in resp.get_all("Contact"):
            for part in c.split(","):
                try:
                    na = NameAddr.parse(part)
                except Exception:  # noqa: BLE001
                    continue
                if na.uri.user == mine.user and na.uri.host == mine.host and (na.uri.port or 5060) == (mine.port or 5060):
                    exp = na.params.get("expires")
                    if exp and exp.isdigit():
                        return int(exp)
        return resp.expires_header or requested

    async def start(self) -> bool:
        self._set_state("registering")
        ok = await self._register_once(self.requested_expires)
        self._task = asyncio.get_running_loop().create_task(self._refresh_loop(), name=f"reg-{self.user}")
        return ok

    async def _refresh_loop(self) -> None:
        backoff = 5.0
        while True:
            if self.state == "registered":
                wait = max(self.granted_expires - min(30, self.granted_expires * 0.2), self.granted_expires * 0.5, 5)
                backoff = 5.0
            else:
                wait = backoff
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(wait)
            try:
                await self._register_once(self.requested_expires)
            except Exception as exc:  # noqa: BLE001
                log.warning("registration refresh failed for %s: %s", self.user, exc)
                self._set_state("failed")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self.state == "registered":
            try:
                await asyncio.wait_for(self._register_once(0), timeout=3.0)
            except (TimeoutError, Exception) as exc:  # noqa: BLE001
                log.warning("unregister %s: %s", self.user, exc)


# ---------------------------------------------------------------------------
# Answerer extensions and inbound calls
# ---------------------------------------------------------------------------


@dataclass
class AnswererExtension:
    user: str
    password: str
    max_calls: int = 5
    audio: AudioSource | None = None
    answer_after: float = 0.0
    index: int = 0
    calls: dict[str, InboundCall] = field(default_factory=dict)
    registration: Registration | None = None
    rejected_busy: int = 0

    @property
    def active_calls(self) -> int:
        return sum(1 for c in self.calls.values() if c.record.state not in (CallState.DONE,))


@dataclass
class RingSet:
    """INVITEs that a simultaneous-ring group delivered for one caller.

    All members wait ``ring_window`` for their siblings, then the extension
    with the fewest active calls (lowest index on ties) answers; the others
    keep ringing and are normally CANCELled by the PBX once the winner
    answers. If no CANCEL arrives within ``loser_grace`` they answer too, so
    sequential ring and single-extension setups still work.
    """

    from_user: str
    t0: float
    members: list[InboundCall] = field(default_factory=list)
    winner: InboundCall | None = None
    key: str | None = None  # correlation header value when the PBX passes it through

    def decide(self) -> InboundCall:
        if self.winner is None:
            alive = [m for m in self.members if m.record.state == CallState.RINGING]
            alive.sort(key=lambda m: (m.ext.active_calls, m.ext.index))
            self.winner = alive[0] if alive else self.members[0]
        return self.winner


class Answerer:
    """Hosts several extensions on one SIP endpoint (pjsua replacement)."""

    def __init__(self, endpoint: SipEndpoint, media_engine: MediaEngine, pbx_addr: Addr, domain: str, extensions: list[AnswererExtension], codecs: list[str] | None = None, record_rx_dir: Path | None = None, on_call: Callable[[CallRecord, str], None] | None = None, register_expires: int = 300, ring_window: float = 0.05, loser_grace: float = 1.0, correlation_header: str | None = "X-Semishigure-Call"):
        self.endpoint = endpoint
        self.media_engine = media_engine
        self.pbx_addr = pbx_addr
        self.domain = domain
        self.extensions = {e.user: e for e in extensions}
        for i, e in enumerate(extensions):
            e.index = i
        self.codecs = list(codecs or ["PCMU", "PCMA"])
        self.record_rx_dir = record_rx_dir
        self.on_call = on_call
        self.register_expires = register_expires
        self.ring_window = ring_window
        self.loser_grace = loser_grace
        self.correlation_header = correlation_header
        self.records: list[CallRecord] = []
        self.rejected_unknown = 0
        self._ring_sets: list[RingSet] = []
        endpoint.request_handler = self._on_request

    def _ring_set_for(self, call: InboundCall) -> RingSet:
        now = asyncio.get_running_loop().time()
        self._ring_sets = [rs for rs in self._ring_sets if now - rs.t0 <= 5.0]
        key = call.invite.get(self.correlation_header) if self.correlation_header else None
        for rs in self._ring_sets:
            if key is not None:
                if rs.key == key:
                    rs.members.append(call)
                    return rs
            elif rs.key is None and rs.from_user == call.record.remote_user and now - rs.t0 <= self.ring_window and rs.winner is None:
                rs.members.append(call)
                return rs
        rs = RingSet(from_user=call.record.remote_user, t0=now, members=[call], key=key)
        self._ring_sets.append(rs)
        return rs

    async def start(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for ext in self.extensions.values():
            ext.registration = Registration(self.endpoint, self.pbx_addr, self.domain, ext.user, ext.password, expires=self.register_expires)
        outcomes = await asyncio.gather(*(ext.registration.start() for ext in self.extensions.values()), return_exceptions=True)
        for ext, ok in zip(self.extensions.values(), outcomes, strict=True):
            results[ext.user] = ok is True
        return results

    async def stop(self) -> None:
        calls = [c for e in self.extensions.values() for c in list(e.calls.values())]
        await asyncio.gather(*(c.hangup("shutdown") for c in calls), return_exceptions=True)
        await asyncio.gather(*(e.registration.stop() for e in self.extensions.values() if e.registration), return_exceptions=True)

    @property
    def active_calls(self) -> list[InboundCall]:
        return [c for e in self.extensions.values() for c in e.calls.values() if c.record.state != CallState.DONE]

    def _on_request(self, req: SipMessage, txn: ServerTransaction, addr: Addr) -> None:
        method = req.method
        if method == "INVITE":
            self._on_invite(req, txn, addr)
        elif method in ("NOTIFY", "INFO", "UPDATE", "MESSAGE", "SUBSCRIBE"):
            txn.respond(SipMessage.response(200))
        else:
            txn.respond(SipMessage.response(405))

    def _on_invite(self, req: SipMessage, txn: ServerTransaction, addr: Addr) -> None:
        try:
            user = SipUri.parse(req.uri or "").user
        except Exception:  # noqa: BLE001
            user = None
        ext = self.extensions.get(user or "") or self.extensions.get(req.to_addr.uri.user or "")
        if ext is None:
            self.rejected_unknown += 1
            txn.respond(SipMessage.response(404))
            return
        if ext.active_calls >= ext.max_calls:
            ext.rejected_busy += 1
            log.info("extension %s busy (%d active); 486", ext.user, ext.active_calls)
            txn.respond(SipMessage.response(486))
            return
        call = InboundCall(self, ext, req, txn, addr)
        ext.calls[call.record.id] = call
        self.records.append(call.record)
        call.ring_set = self._ring_set_for(call)
        call.handle()

    def _call_ended(self, call: InboundCall) -> None:
        call.ext.calls.pop(call.record.id, None)


class InboundCall:
    def __init__(self, answerer: Answerer, ext: AnswererExtension, req: SipMessage, txn: ServerTransaction, addr: Addr):
        self.answerer = answerer
        self.ext = ext
        self.invite = req
        self.txn = txn
        self.addr = addr
        self.endpoint = answerer.endpoint
        self.local_tag = new_tag()
        self.dialog: Dialog | None = None
        self.media: MediaSession | None = None
        self.ended = asyncio.Event()
        self.ring_set: RingSet | None = None
        self._answer_task: asyncio.Task | None = None
        caller = req.from_addr.uri.user or ""
        self.record = CallRecord(role=CallRole.ANSWERER, local_user=ext.user, remote_user=caller, extension=ext.user, on_event=answerer.on_call)
        self.record.sip_call_id = req.call_id
        self.record.t_invite = self.record.mark("invite_received")

    def _contact(self) -> str:
        return f"<{self.endpoint.contact_uri(self.ext.user)}>"

    def handle(self) -> None:
        rec = self.record
        offer = parse_sdp(self.invite.body) if self.invite.body else None
        chosen = choose_codec(offer, self.answerer.codecs) if offer else None
        if offer is None or chosen is None:
            self.txn.respond(SipMessage.response(488))
            rec.finish("no_common_codec", 488)
            self.answerer._call_ended(self)
            return
        codec, pt = chosen
        rec.codec = codec
        record_path = None
        if self.answerer.record_rx_dir is not None:
            record_path = self.answerer.record_rx_dir / f"rx_{self.ext.user}_{rec.id}.wav"
        self.media = self.answerer.media_engine.create_session(self.endpoint.local_ip, self.ext.audio, record_path)
        rec.media = self.media.stats()
        self.media.set_remote(offer.ip, offer.port, codec, pt)
        self.dialog = Dialog.from_uas(self.invite, self.local_tag, self.endpoint.contact_uri(self.ext.user))
        self.endpoint.register_dialog(self.dialog, self)
        self.txn.on_cancel = self._on_cancel
        self.txn.on_ack = self._on_invite_ack
        self.txn.on_ack_timeout = self._on_ack_timeout
        ringing = SipMessage.response(180)
        ringing.add("To", f"{self.invite.get('To')};tag={self.local_tag}")
        ringing.add("Contact", self._contact())
        self.txn.respond(ringing)
        rec.t_180 = rec.mark("tx_180")
        rec.set_state(CallState.RINGING)
        self._answer_task = asyncio.get_running_loop().create_task(self._answer_later(), name=f"answer-{rec.id}")

    async def _answer_later(self) -> None:
        await asyncio.sleep(self.answerer.ring_window)
        if self.ring_set is not None and self.ring_set.decide() is not self:
            self.record.mark("ring_set_loser")
            await asyncio.sleep(self.answerer.loser_grace)
        if self.ext.answer_after > 0:
            await asyncio.sleep(self.ext.answer_after)
        if self.record.state == CallState.DONE or self.txn.cancelled:
            return
        assert self.media is not None and self.record.codec
        ok = SipMessage.response(200)
        ok.add("To", f"{self.invite.get('To')};tag={self.local_tag}")
        ok.add("Contact", self._contact())
        ok.add("Content-Type", "application/sdp")
        offer = parse_sdp(self.invite.body)
        dtmf_pt = offer.telephone_event_pt() if offer else None
        ok.body = build_answer(self.media.local_ip, self.media.local_port, self.record.codec, dtmf_pt)
        self.txn.respond(ok)
        self.record.t_200 = self.record.mark("tx_200")

    def _on_invite_ack(self, ack: SipMessage) -> None:
        rec = self.record
        if rec.state == CallState.DONE:
            return
        rec.t_ack = rec.mark("ack_received")
        rec.t_established = rec.mark("established")
        rec.set_state(CallState.ESTABLISHED)
        if self.media is not None:
            self.media.start()

    def _on_cancel(self, cancel: SipMessage) -> None:
        self.record.mark("cancel_received")
        self._end("cancelled", 487)

    def _on_ack_timeout(self) -> None:
        if self.record.state != CallState.DONE:
            asyncio.get_running_loop().create_task(self.hangup("no_ack"))

    async def hangup(self, reason: str = "local_bye") -> None:
        rec = self.record
        if rec.state == CallState.DONE:
            return
        answered = self.txn.final is not None and 200 <= (self.txn.final.status or 0) < 300
        if (rec.state == CallState.ESTABLISHED or answered) and self.dialog is not None:
            rec.set_state(CallState.TERMINATING)
            bye = self.dialog.create_request("BYE", self.endpoint.local_ip, self.endpoint.local_port, transport=self.endpoint.transport_name)
            rec.t_bye = rec.mark("bye_sent")
            txn = self.endpoint.send_request(bye, self.dialog.next_hop())
            try:
                await txn.wait_final(timeout=6.0)
            except TimeoutError:
                pass
            self._end(reason, txn.final.status if txn.final else None)
            return
        if rec.state == CallState.RINGING and self.txn.final is None:
            self.txn.respond(SipMessage.response(480))
            self._end(reason, 480)
            return
        self._end(reason)

    def _end(self, reason: str, status: int | None = None) -> None:
        rec = self.record
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()
        if self.media is not None:
            self.media.stop()
        if self.dialog is not None:
            self.endpoint.unregister_dialog(self.dialog)
        rec.finish(reason, status)
        self.ended.set()
        self.answerer._call_ended(self)

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
            resp.add("Contact", self._contact())
            resp.add("Content-Type", "application/sdp")
            if request.body and self.media is not None and rec.codec:
                offer = parse_sdp(request.body)
                if offer:
                    chosen = choose_codec(offer, [rec.codec])
                    if chosen:
                        self.media.set_remote(offer.ip, offer.port, chosen[0], chosen[1])
                resp.body = build_answer(self.media.local_ip, self.media.local_port, rec.codec, 101)
            txn.respond(resp)
            rec.mark("reinvite_answered")
        else:
            txn.respond(SipMessage.response(200))

    def on_ack(self, ack: SipMessage) -> None:
        pass
