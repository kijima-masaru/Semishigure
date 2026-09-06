"""SIP dialog state (RFC 3261 §12): tags, CSeq, route set, remote target."""

from __future__ import annotations

from dataclasses import dataclass, field

from semishigure.sip.message import NameAddr, SipMessage, SipUri, new_branch


@dataclass
class Dialog:
    call_id: str
    local_tag: str
    remote_tag: str
    local_uri: str  # full From/To header value without tag
    remote_uri: str
    remote_target: str  # Contact URI of the peer
    route_set: list[str] = field(default_factory=list)
    local_cseq: int = 0
    remote_cseq: int = 0
    local_contact: str = ""
    established: bool = False

    @property
    def id(self) -> tuple[str, str, str]:
        return (self.call_id, self.local_tag, self.remote_tag)

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_uac(cls, request: SipMessage, response: SipMessage, local_contact: str) -> Dialog:
        """Create a dialog on the UAC side from an INVITE and a 1xx/2xx with a To tag."""
        frm = request.from_addr
        to = response.to_addr
        contact = response.contact
        remote_target = str(contact.uri) if contact else request.uri or ""
        # Route set = Record-Route in reverse order (RFC 3261 §12.1.2)
        route_set = list(reversed(response.record_routes))
        d = cls(
            call_id=request.call_id,
            local_tag=frm.tag or "",
            remote_tag=to.tag or "",
            local_uri=_strip_tag(request.get("From") or ""),
            remote_uri=_strip_tag(response.get("To") or ""),
            remote_target=remote_target,
            route_set=route_set,
            local_cseq=request.cseq[0],
            local_contact=local_contact,
        )
        return d

    @classmethod
    def from_uas(cls, request: SipMessage, local_tag: str, local_contact: str) -> Dialog:
        """Create a dialog on the UAS side when sending a tagged response to an INVITE."""
        frm = request.from_addr
        contact = request.contact
        remote_target = str(contact.uri) if contact else _strip_tag(request.get("From") or "")
        route_set = list(request.record_routes)  # same order as received (RFC 3261 §12.1.1)
        return cls(
            call_id=request.call_id,
            local_tag=local_tag,
            remote_tag=frm.tag or "",
            local_uri=_strip_tag(request.get("To") or ""),
            remote_uri=_strip_tag(request.get("From") or ""),
            remote_target=remote_target,
            route_set=route_set,
            local_cseq=0,
            remote_cseq=request.cseq[0],
            local_contact=local_contact,
        )

    # -- requests -------------------------------------------------------------

    def create_request(self, method: str, via_host: str, via_port: int, cseq: int | None = None, transport: str = "UDP") -> SipMessage:
        if cseq is None:
            self.local_cseq += 1
            cseq = self.local_cseq
        req_uri, routes = self._request_uri_and_routes()
        req = SipMessage.request(method, req_uri)
        req.add("Via", f"SIP/2.0/{transport} {via_host}:{via_port};branch={new_branch()};rport")
        for r in routes:
            req.add("Route", r)
        req.add("From", f"{self.local_uri};tag={self.local_tag}")
        req.add("To", f"{self.remote_uri};tag={self.remote_tag}" if self.remote_tag else self.remote_uri)
        req.add("Call-ID", self.call_id)
        req.add("CSeq", f"{cseq} {method}")
        req.add("Max-Forwards", "70")
        if method != "ACK" and self.local_contact:
            req.add("Contact", f"<{self.local_contact}>")
        return req

    def _request_uri_and_routes(self) -> tuple[str, list[str]]:
        if not self.route_set:
            return self.remote_target, []
        first = NameAddr.parse(self.route_set[0])
        if "lr" in first.uri.params:
            return self.remote_target, list(self.route_set)
        # strict router: request-URI = first route, remote target appended
        return str(first.uri), list(self.route_set[1:]) + [f"<{self.remote_target}>"]

    def next_hop(self) -> tuple[str, int]:
        """Where in-dialog requests are sent."""
        if self.route_set:
            first = NameAddr.parse(self.route_set[0])
            return first.uri.hostport
        return SipUri.parse(self.remote_target).hostport


def _strip_tag(header_value: str) -> str:
    parts = [p for p in header_value.split(";") if not p.strip().lower().startswith("tag=")]
    return ";".join(parts).strip()
