"""HTTP Digest authentication for SIP (RFC 3261 §22 / RFC 2617 / RFC 7616 subset).

Supports MD5 and SHA-256 algorithms with qop=auth (and no qop), which covers
FreeSWITCH and Asterisk defaults.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

_PARAM_RE = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


@dataclass
class DigestChallenge:
    realm: str
    nonce: str
    algorithm: str = "MD5"
    qop: list[str] = field(default_factory=list)
    opaque: str | None = None
    stale: bool = False
    raw: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, header_value: str) -> DigestChallenge:
        scheme, _, rest = header_value.strip().partition(" ")
        if scheme.lower() != "digest":
            raise ValueError(f"unsupported auth scheme: {scheme}")
        params: dict[str, str] = {}
        for m in _PARAM_RE.finditer(rest):
            key = m.group(1).lower()
            val = m.group(2) if m.group(2) is not None else m.group(3)
            params[key] = val.replace('\\"', '"')
        if "realm" not in params or "nonce" not in params:
            raise ValueError("digest challenge lacks realm/nonce")
        qop = [q.strip() for q in params.get("qop", "").split(",") if q.strip()]
        return cls(
            realm=params["realm"],
            nonce=params["nonce"],
            algorithm=params.get("algorithm", "MD5"),
            qop=qop,
            opaque=params.get("opaque"),
            stale=params.get("stale", "false").lower() == "true",
            raw=params,
        )


class DigestClient:
    """Keeps nonce-count state per (realm, nonce)."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._nc: dict[tuple[str, str], int] = {}

    def _hash(self, algorithm: str):
        alg = algorithm.upper().removesuffix("-SESS")
        if alg == "MD5":
            return hashlib.md5
        if alg == "SHA-256":
            return hashlib.sha256
        raise ValueError(f"unsupported digest algorithm: {algorithm}")

    def authorization(self, challenge: DigestChallenge, method: str, uri: str, body: bytes = b"") -> str:
        h = self._hash(challenge.algorithm)

        def H(s: str) -> str:
            return h(s.encode()).hexdigest()

        ha1 = H(f"{self.username}:{challenge.realm}:{self.password}")
        if challenge.algorithm.upper().endswith("-SESS"):
            cnonce_sess = os.urandom(8).hex()
            ha1 = H(f"{ha1}:{challenge.nonce}:{cnonce_sess}")
        qop = None
        if "auth" in challenge.qop:
            qop = "auth"
        elif "auth-int" in challenge.qop:
            qop = "auth-int"
        if qop == "auth-int":
            ha2 = H(f"{method}:{uri}:{H(body.decode('latin-1'))}")
        else:
            ha2 = H(f"{method}:{uri}")
        parts = [
            f'username="{self.username}"',
            f'realm="{challenge.realm}"',
            f'nonce="{challenge.nonce}"',
            f'uri="{uri}"',
        ]
        if qop:
            key = (challenge.realm, challenge.nonce)
            self._nc[key] = self._nc.get(key, 0) + 1
            nc = f"{self._nc[key]:08x}"
            cnonce = os.urandom(8).hex()
            response = H(f"{ha1}:{challenge.nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            parts += [f'response="{response}"', f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            response = H(f"{ha1}:{challenge.nonce}:{ha2}")
            parts.append(f'response="{response}"')
        parts.append(f"algorithm={challenge.algorithm}")
        if challenge.opaque is not None:
            parts.append(f'opaque="{challenge.opaque}"')
        return "Digest " + ", ".join(parts)


def challenge_from_response(status: int, get_header) -> tuple[DigestChallenge, str] | None:
    """Return (challenge, header-name-to-send) for a 401/407 response."""
    if status == 401:
        v = get_header("WWW-Authenticate")
        return (DigestChallenge.parse(v), "Authorization") if v else None
    if status == 407:
        v = get_header("Proxy-Authenticate")
        return (DigestChallenge.parse(v), "Proxy-Authorization") if v else None
    return None
