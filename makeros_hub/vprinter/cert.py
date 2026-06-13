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

    ca_cert_path = cert_dir / "ca.crt"
    ca_key_path = cert_dir / "ca.key"
    ca_key, ca_cert = _load_or_create_ca(ca_cert_path, ca_key_path)

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
        ca_key=ca_key_path,
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


def _load_or_create_ca(ca_cert_path: Path, ca_key_path: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    if ca_cert_path.exists() and ca_key_path.exists():
        try:
            key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
            cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
            if isinstance(key, rsa.RSAPrivateKey) and _is_expected_ca(cert):
                os.chmod(ca_key_path, 0o600)
                return key, cert
        except Exception:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
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


def _is_expected_ca(cert: x509.Certificate) -> bool:
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not cn or cn[0].value != CA_COMMON_NAME:
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
