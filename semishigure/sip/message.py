"""SIP message model: parsing and serialisation (RFC 3261 subset).

Only what a UAC/UAS load generator needs: request/response lines, headers
(with compact-form expansion), Via/From/To/CSeq helpers and a raw body.
Header order is preserved because the load scenario may want custom
headers emitted in a fixed order.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field

SIP_VERSION = "SIP/2.0"
CRLF = "\r\n"

# RFC 3261 §7.3.3 compact forms
_COMPACT = {
    "v": "Via",
    "f": "From",
    "t": "To",
    "i": "Call-ID",
    "m": "Contact",
    "l": "Content-Length",
    "c": "Content-Type",
    "e": "Content-Encoding",
    "s": "Subject",
    "k": "Supported",
}

# Canonical capitalisation for headers we emit/inspect.
_CANON = {
    "via": "Via",
    "from": "From",
    "to": "To",
    "call-id": "Call-ID",
    "cseq": "CSeq",
    "contact": "Contact",
    "content-length": "Content-Length",
    "content-type": "Content-Type",
    "max-forwards": "Max-Forwards",
    "expires": "Expires",
    "www-authenticate": "WWW-Authenticate",
    "proxy-authenticate": "Proxy-Authenticate",
    "authorization": "Authorization",
    "proxy-authorization": "Proxy-Authorization",
    "record-route": "Record-Route",
    "route": "Route",
    "user-agent": "User-Agent",
    "allow": "Allow",
    "supported": "Supported",
    "subject": "Subject",
    "reason": "Reason",
    "min-expires": "Min-Expires",
    "event": "Event",
    "subscription-state": "Subscription-State",
    "p-asserted-identity": "P-Asserted-Identity",
}

RESPONSE_PHRASES = {
    100: "Trying",
    180: "Ringing",
    183: "Session Progress",
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    415: "Unsupported Media Type",
    480: "Temporarily Unavailable",
    481: "Call/Transaction Does Not Exist",
    486: "Busy Here",
    487: "Request Terminated",
    488: "Not Acceptable Here",
    500: "Server Internal Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    603: "Decline",
}

_ALNUM = string.ascii_letters + string.digits


def canonical_header_name(name: str) -> str:
    n = name.strip()
    if len(n) == 1:
        n = _COMPACT.get(n.lower(), n)
    return _CANON.get(n.lower(), n)


def random_token(length: int = 16) -> str:
    return "".join(random.choices(_ALNUM, k=length))


def new_branch() -> str:
    """RFC 3261 §8.1.1.7 magic cookie + unique suffix."""
    return "z9hG4bK" + random_token(20)


def new_tag() -> str:
    return random_token(12)


def new_call_id(host: str) -> str:
    return f"{random_token(24)}@{host}"


class SipParseError(ValueError):
    pass


# ---------------------------------------------------------------------------
# URIs and name-addr
# ---------------------------------------------------------------------------

_URI_RE = re.compile(
    r"^(?P<scheme>sips?):(?:(?P<user>[^@;]+)@)?(?P<host>\[[^\]]+\]|[^:;?]+)(?::(?P<port>\d+))?"
    r"(?P<params>(?:;[^;?]*)*)(?:\?(?P<headers>.*))?$"
)


@dataclass
class SipUri:
    user: str | None
    host: str
    port: int | None = None
    scheme: str = "sip"
    params: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> SipUri:
        m = _URI_RE.match(text.strip())
        if not m:
            raise SipParseError(f"invalid SIP URI: {text!r}")
        params: dict[str, str | None] = {}
        for p in filter(None, m.group("params").split(";")):
            if "=" in p:
                k, v = p.split("=", 1)
                params[k] = v
            else:
                params[p] = None
        return cls(
            user=m.group("user"),
            host=m.group("host"),
            port=int(m.group("port")) if m.group("port") else None,
            scheme=m.group("scheme"),
            params=params,
        )

    def __str__(self) -> str:
        s = f"{self.scheme}:"
        if self.user:
            s += f"{self.user}@"
        s += self.host
        if self.port:
            s += f":{self.port}"
        for k, v in self.params.items():
            s += f";{k}" if v is None else f";{k}={v}"
        return s

    @property
    def hostport(self) -> tuple[str, int]:
        return self.host, self.port or 5060


@dataclass
class NameAddr:
    """From / To / Contact / Route style value: [display] <uri>;params."""

    uri: SipUri
    display: str | None = None
    params: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> NameAddr:
        text = text.strip()
        display = None
        if "<" in text:
            before, rest = text.split("<", 1)
            uri_text, after = rest.split(">", 1)
            display = before.strip().strip('"') or None
            param_text = after
        else:
            # addr-spec form: params after ';' belong to the header, not the URI
            if ";" in text:
                uri_text, param_text = text.split(";", 1)
                param_text = ";" + param_text
            else:
                uri_text, param_text = text, ""
        params: dict[str, str | None] = {}
        for p in filter(None, param_text.split(";")):
            p = p.strip()
            if not p:
                continue
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.strip()] = v.strip()
            else:
                params[p] = None
        return cls(uri=SipUri.parse(uri_text), display=display, params=params)

    def __str__(self) -> str:
        s = f'"{self.display}" ' if self.display else ""
        s += f"<{self.uri}>"
        for k, v in self.params.items():
            s += f";{k}" if v is None else f";{k}={v}"
        return s

    @property
    def tag(self) -> str | None:
        return self.params.get("tag")


# ---------------------------------------------------------------------------
# Via
# ---------------------------------------------------------------------------


@dataclass
class Via:
    host: str
    port: int | None
    transport: str = "UDP"
    params: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> Via:
        # SIP/2.0/UDP host:port;branch=...;rport
        proto, _, rest = text.strip().partition(" ")
        transport = proto.split("/")[-1].upper()
        parts = rest.split(";")
        hp = parts[0].strip()
        if hp.startswith("["):
            host, _, port = hp.rpartition("]:")
            host = host + "]" if port else hp
        elif ":" in hp:
            host, port = hp.rsplit(":", 1)
        else:
            host, port = hp, ""
        params: dict[str, str | None] = {}
        for p in parts[1:]:
            p = p.strip()
            if not p:
                continue
            if "=" in p:
                k, v = p.split("=", 1)
                params[k] = v
            else:
                params[p] = None
        return cls(host=host, port=int(port) if port else None, transport=transport, params=params)

    def __str__(self) -> str:
        s = f"SIP/2.0/{self.transport} {self.host}"
        if self.port:
            s += f":{self.port}"
        for k, v in self.params.items():
            s += f";{k}" if v is None else f";{k}={v}"
        return s

    @property
    def branch(self) -> str | None:
        return self.params.get("branch")


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class SipMessage:
    """A SIP request or response. Headers are an ordered list of (name, value)."""

    method: str | None = None  # request
    uri: str | None = None
    status: int | None = None  # response
    reason: str | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    # -- construction helpers ------------------------------------------------

    @classmethod
    def request(cls, method: str, uri: str, headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> SipMessage:
        return cls(method=method, uri=uri, headers=list(headers or []), body=body)

    @classmethod
    def response(cls, status: int, reason: str | None = None, headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> SipMessage:
        return cls(status=status, reason=reason or RESPONSE_PHRASES.get(status, "Unknown"), headers=list(headers or []), body=body)

    @property
    def is_request(self) -> bool:
        return self.method is not None

    @property
    def is_response(self) -> bool:
        return self.status is not None

    # -- header access --------------------------------------------------------

    def get(self, name: str, default: str | None = None) -> str | None:
        key = canonical_header_name(name).lower()
        for n, v in self.headers:
            if n.lower() == key:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        key = canonical_header_name(name).lower()
        return [v for n, v in self.headers if n.lower() == key]

    def set(self, name: str, value: str) -> None:
        """Replace the first occurrence (or append)."""
        name = canonical_header_name(name)
        key = name.lower()
        for i, (n, _) in enumerate(self.headers):
            if n.lower() == key:
                self.headers[i] = (name, value)
                return
        self.headers.append((name, value))

    def add(self, name: str, value: str) -> None:
        self.headers.append((canonical_header_name(name), value))

    def remove(self, name: str) -> None:
        key = canonical_header_name(name).lower()
        self.headers = [(n, v) for n, v in self.headers if n.lower() != key]

    # -- typed accessors ------------------------------------------------------

    @property
    def call_id(self) -> str:
        return self.get("Call-ID") or ""

    @property
    def cseq(self) -> tuple[int, str]:
        v = self.get("CSeq") or "0 UNKNOWN"
        num, _, method = v.strip().partition(" ")
        return int(num), method.strip().upper()

    @property
    def cseq_method(self) -> str:
        return self.cseq[1]

    @property
    def from_addr(self) -> NameAddr:
        return NameAddr.parse(self.get("From") or "")

    @property
    def to_addr(self) -> NameAddr:
        return NameAddr.parse(self.get("To") or "")

    @property
    def top_via(self) -> Via | None:
        v = self.get("Via")
        return Via.parse(v.split(",")[0]) if v else None

    @property
    def branch(self) -> str | None:
        via = self.top_via
        return via.branch if via else None

    @property
    def contact(self) -> NameAddr | None:
        c = self.get("Contact")
        if not c or c.strip() == "*":
            return None
        return NameAddr.parse(c.split(",")[0])

    @property
    def content_type(self) -> str:
        return (self.get("Content-Type") or "").lower()

    @property
    def record_routes(self) -> list[str]:
        out: list[str] = []
        for v in self.get_all("Record-Route"):
            out.extend(x.strip() for x in v.split(",") if x.strip())
        return out

    @property
    def expires_header(self) -> int | None:
        v = self.get("Expires")
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    # -- serialisation --------------------------------------------------------

    def to_bytes(self) -> bytes:
        if self.is_request:
            start = f"{self.method} {self.uri} {SIP_VERSION}"
        else:
            start = f"{SIP_VERSION} {self.status} {self.reason}"
        lines = [start]
        has_cl = False
        for n, v in self.headers:
            if n.lower() == "content-length":
                has_cl = True
                v = str(len(self.body))
            lines.append(f"{n}: {v}")
        if not has_cl:
            lines.append(f"Content-Length: {len(self.body)}")
        head = CRLF.join(lines) + CRLF + CRLF
        return head.encode("utf-8") + self.body

    def __str__(self) -> str:
        return self.to_bytes().decode("utf-8", errors="replace")

    @property
    def summary(self) -> str:
        if self.is_request:
            return f"{self.method} {self.uri}"
        return f"{self.status} {self.reason} ({self.cseq_method})"

    # -- parsing -------------------------------------------------------------

    @classmethod
    def parse(cls, data: bytes) -> SipMessage:
        # Tolerate leading CRLF keep-alives
        data = data.lstrip(b"\r\n")
        if not data:
            raise SipParseError("empty datagram")
        sep = data.find(b"\r\n\r\n")
        if sep < 0:
            # Some stacks send LF only; be lenient
            sep = data.find(b"\n\n")
            if sep < 0:
                raise SipParseError("no header/body separator")
            head_bytes, body = data[:sep], data[sep + 2 :]
        else:
            head_bytes, body = data[:sep], data[sep + 4 :]
        head = head_bytes.decode("utf-8", errors="replace")
        raw_lines = head.split("\n")
        # unfold continuation lines
        lines: list[str] = []
        for ln in raw_lines:
            ln = ln.rstrip("\r")
            if ln[:1] in (" ", "\t") and lines:
                lines[-1] += " " + ln.strip()
            else:
                lines.append(ln)
        start = lines[0]
        msg = cls()
        if start.startswith(SIP_VERSION):
            parts = start.split(" ", 2)
            if len(parts) < 2:
                raise SipParseError(f"bad status line: {start!r}")
            msg.status = int(parts[1])
            msg.reason = parts[2] if len(parts) > 2 else RESPONSE_PHRASES.get(msg.status, "")
        else:
            parts = start.split(" ")
            if len(parts) != 3 or parts[2] != SIP_VERSION:
                raise SipParseError(f"bad request line: {start!r}")
            msg.method = parts[0].upper()
            msg.uri = parts[1]
        for ln in lines[1:]:
            if not ln.strip():
                continue
            name, _, value = ln.partition(":")
            msg.headers.append((canonical_header_name(name), value.strip()))
        cl = msg.get("Content-Length")
        if cl is not None:
            try:
                n = int(cl)
                body = body[:n]
            except ValueError:
                pass
        msg.body = body
        return msg
