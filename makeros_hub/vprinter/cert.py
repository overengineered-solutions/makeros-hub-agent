from __future__ import annotations

import ipaddress
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


CA_COMMON_NAME = "Virtual Printer CA"


def ca_common_name(ca_id: str) -> str:
    """The subject of this hub's ONE virtual-printer CA.

    Every VP used to mint its own CA with the identical subject "Virtual Printer CA". A TLS client looks a CA up BY
    SUBJECT, so with four indistinguishable ones in OrcaSlicer's printer.cer the client took the first match and every
    other VP's leaf failed "certificate signature failure" — the whole reason only one model could ever connect
    (reproduced 2026-08-30; whichever CA sat first in the bundle won, and it happened to be an A1 mini's. OrcaSlicer
    reports it only as "Connect PS H2D (Operator) failed! [SN:..., code=-1]").

    One CA now signs every virtual printer on the hub, so there is nothing to collide: the member trusts a single
    certificate instead of one per printer. The VPs themselves are unchanged — still one per MODEL, each with its own
    serial, IP and Device-tab entry, so a member still picks the machine they mean.

    The subject carries a per-hub id minted once, so a member who belongs to TWO makerspaces does not reintroduce the
    same collision between the two shops' CAs.
    """
    return f"{CA_COMMON_NAME} {ca_id}"
CIPHER_STRING = "DEFAULT:AES256-GCM-SHA384:AES128-GCM-SHA256"


@dataclass(frozen=True)
class CertBundle:
    cert_dir: Path
    ca_cert: Path
    ca_key: Path
    leaf_chain: Path
    leaf_key: Path
    ca_fingerprint_sha256: str


def ensure_certificates(
    base_dir: Path,
    serial: str,
    bind_ips: str | list[str] | None = None,
    *,
    ip: str | None = None,
) -> CertBundle:
    if bind_ips is None:
        bind_ips = ip
    if bind_ips is None:
        raise ValueError("at least one bind IP is required")
    cert_dir = base_dir / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)

    # ONE CA per hub, shared by every virtual printer, kept beside the per-serial directories. The public cert is also
    # copied into this VP's own certs/ dir so read_vp_ca and the heartbeat upload keep working unchanged; the KEY never
    # leaves the shared directory.
    ca_dir = base_dir.parent / "ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = _load_or_create_ca(ca_dir / "ca.crt", ca_dir / "ca.key", ca_dir / "ca.id")
    ca_cert_path = cert_dir / "ca.crt"
    ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    ips = [bind_ips] if isinstance(bind_ips, str) else list(bind_ips)
    leaf_key, leaf_cert = _create_leaf(ca_key, ca_cert, serial, ips)
    safe_serial = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in serial)
    leaf_key_path = cert_dir / f"{safe_serial}.key"
    leaf_chain_path = cert_dir / f"{safe_serial}.chain.crt"
    _write_private_key(leaf_key_path, leaf_key)
    leaf_chain_path.write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )

    return CertBundle(
        cert_dir=cert_dir,
        ca_cert=ca_cert_path,
        ca_key=ca_dir / "ca.key",   # the shared CA key never leaves its own directory
        leaf_chain=leaf_chain_path,
        leaf_key=leaf_key_path,
        ca_fingerprint_sha256=_fingerprint(ca_cert),
    )


def create_server_ssl_context(
    bundle: CertBundle,
    *,
    tls12_only: bool = False,
    max_tls12: bool = False,
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(bundle.leaf_chain), str(bundle.leaf_key))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if tls12_only or max_tls12:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers(CIPHER_STRING)
    return context


def _ca_id(path: Path) -> str:
    """A stable random id for THIS hub's CA, minted once. Keeps two shops' CAs distinguishable in one member's trust
    store — the same failure the per-VP collision caused, one level up."""
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    minted = os.urandom(4).hex()
    path.write_text(minted)
    return minted


def _load_or_create_ca(ca_cert_path: Path, ca_key_path: Path, ca_id_path: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    ca_id = _ca_id(ca_id_path)
    if ca_cert_path.exists() and ca_key_path.exists():
        try:
            key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
            cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
            # A CA carrying the OLD shared subject fails this check and is re-minted — that is the migration.
            # A CA from before this change carries the old shared subject, fails here, and is re-minted.
            if isinstance(key, rsa.RSAPrivateKey) and _is_expected_ca(cert, ca_id):
                os.chmod(ca_key_path, 0o600)
                return key, cert
        except Exception:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ca_common_name(ca_id))])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 20))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_private_key(ca_key_path, key)
    return key, cert


def _create_leaf(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    serial: str,
    ips: list[str],
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)])
    san_items: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(serial),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for ip in ips:
        san_items.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san_items), critical=False)
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    return key, cert


def _is_expected_ca(cert: x509.Certificate, ca_id: str) -> bool:
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not cn or cn[0].value != ca_common_name(ca_id):
        return False
    try:
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return False
    return basic.ca is True and basic.path_length == 0


def _write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)


def _fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{byte:02X}" for byte in digest)


def read_vp_ca(base_dir: Path, serial: str) -> tuple[str, str] | None:
    """Read the on-disk CA cert for a VP serial and return
    `(pem_text, fingerprint_sha256_colons_hex)` or None when the file is
    missing or unreadable. Pure-IO helper used by the agent heartbeat to
    upload the CA to the cloud (V4 Slice 2 — managed CA delivery).

    Path matches what `ensure_certificates` writes:
        `<base_dir>/<safe_serial>/certs/ca.crt`
    """
    safe_serial = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in serial)
    ca_path = base_dir / safe_serial / "certs" / "ca.crt"
    try:
        pem_bytes = ca_path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return None
    try:
        cert = x509.load_pem_x509_certificate(pem_bytes)
    except Exception:  # noqa: BLE001 - a malformed file is treated as missing
        return None
    return pem_bytes.decode("ascii", errors="strict"), _fingerprint(cert)
