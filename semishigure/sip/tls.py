"""TLS contexts for the SIP stream transport.

Client side: verification on by default (system CA store, or ``tls_ca``); a
development PBX with a self-signed certificate needs ``tls_verify: false``.
Server side (the answerer's listener): a self-signed certificate generated on
first use under ``$SEMISHIGURE_HOME/tls`` unless a cert/key pair is configured.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import ssl
from pathlib import Path

from semishigure.secrets import DEFAULT_DIR

log = logging.getLogger(__name__)


def client_context(verify: bool = True, ca_file: str | Path | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def server_context(cert_file: str | Path | None = None, key_file: str | Path | None = None, home: Path | None = None, hostnames: list[str] | None = None) -> ssl.SSLContext:
    if not cert_file or not key_file:
        cert_file, key_file = ensure_self_signed(home or DEFAULT_DIR, hostnames or [])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_file), str(key_file))
    return ctx


def ensure_self_signed(home: Path, hostnames: list[str], days: int = 3650) -> tuple[Path, Path]:
    """Create (once) a self-signed certificate for the answerer's TLS listener."""
    tls_dir = home / "tls"
    cert, key = tls_dir / "semishigure.crt", tls_dir / "semishigure.key"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    tls_dir.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "semishigure")])
    sans: list[x509.GeneralName] = [x509.DNSName("semishigure"), x509.DNSName("localhost")]
    for h in hostnames:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    key.write_bytes(private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        key.chmod(0o600)
    except OSError:
        pass
    log.info("generated self-signed TLS certificate %s", cert)
    return cert, key
