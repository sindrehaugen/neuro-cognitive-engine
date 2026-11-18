#!/usr/bin/env python3
"""Generate an ephemeral internal mTLS PKI for Caddy <-> Admin transport.

Outputs:
  - ca.crt     Self-signed internal Root CA certificate
  - admin.crt  Server leaf certificate for ``admin`` / ``localhost`` / ``127.0.0.1``
  - admin.key  RSA-2048 private key for ``admin``
  - caddy.crt  Client leaf certificate carrying SAN ``DNS:caddy.internal.nce``
  - caddy.key  RSA-2048 private key for ``caddy``

Files are written to ``deploy/certs/internal-mtls`` (gitignored and dockerignored).
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "certs" / "internal-mtls"


def _gen_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_pem(path: Path, data: bytes, *, mode: int) -> None:
    path.write_bytes(data)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def generate_internal_mtls_bundle(
    out_dir: Path = DEFAULT_OUT_DIR,
    *,
    validity_days: int = 90,
    client_san: str = "caddy.internal.nce",
) -> dict[str, Path]:
    """Create a Root CA, Admin server cert/key, and Caddy client cert/key under *out_dir*."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc)
    not_before = now - dt.timedelta(minutes=5)
    not_after = now + dt.timedelta(days=max(1, validity_days))

    ca_key = _gen_rsa_key()
    ca_name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NCE Internal Zero-Trust"),
            x509.NameAttribute(NameOID.COMMON_NAME, "nce-internal-ca"),
        ]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    admin_key = _gen_rsa_key()
    admin_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "admin")])
    admin_cert = (
        x509.CertificateBuilder()
        .subject_name(admin_name)
        .issuer_name(ca_name)
        .public_key(admin_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
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
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("admin"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    caddy_key = _gen_rsa_key()
    caddy_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, client_san)])
    caddy_cert = (
        x509.CertificateBuilder()
        .subject_name(caddy_name)
        .issuer_name(ca_name)
        .public_key(caddy_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
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
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(client_san)]),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    paths = {
        "ca_cert": out_dir / "ca.crt",
        "admin_cert": out_dir / "admin.crt",
        "admin_key": out_dir / "admin.key",
        "caddy_cert": out_dir / "caddy.crt",
        "caddy_key": out_dir / "caddy.key",
    }

    _write_pem(paths["ca_cert"], ca_cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    _write_pem(paths["admin_cert"], admin_cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    _write_pem(
        paths["admin_key"],
        admin_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    _write_pem(paths["caddy_cert"], caddy_cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    _write_pem(
        paths["caddy_key"],
        caddy_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    return paths


def _cert_not_before_utc(cert: x509.Certificate) -> dt.datetime:
    val = getattr(cert, "not_valid_before_utc", None)
    if val is not None:
        return val
    return cert.not_valid_before.replace(tzinfo=dt.timezone.utc)


def _cert_not_after_utc(cert: x509.Certificate) -> dt.datetime:
    val = getattr(cert, "not_valid_after_utc", None)
    if val is not None:
        return val
    return cert.not_valid_after.replace(tzinfo=dt.timezone.utc)


def check_internal_mtls_expiry(
    out_dir: Path = DEFAULT_OUT_DIR,
    *,
    min_days: int = 14,
) -> tuple[bool, list[str]]:
    """Verify ``ca.crt``, ``admin.crt``, and ``caddy.crt`` exist and stay valid >= *min_days*."""
    out_dir = Path(out_dir)
    now = dt.datetime.now(dt.timezone.utc)
    threshold = now + dt.timedelta(days=max(0, min_days))
    issues: list[str] = []

    for fname in ("ca.crt", "admin.crt", "caddy.crt"):
        cert_path = out_dir / fname
        if not cert_path.is_file():
            issues.append(f"Missing mTLS certificate: {cert_path}")
            continue
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except Exception as exc:
            issues.append(f"Invalid PEM certificate {cert_path}: {exc}")
            continue
        not_before = _cert_not_before_utc(cert)
        not_after = _cert_not_after_utc(cert)
        if not_before > now:
            issues.append(
                f"Certificate {fname} is not yet valid (not_before={not_before.isoformat()})"
            )
        elif not_after < threshold:
            remaining = not_after - now
            issues.append(
                f"Certificate {fname} expires in {remaining.days}d (< {max(0, min_days)}d required)"
            )

    return len(issues) == 0, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate internal mTLS certificates for NCE.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Destination directory for CA, admin server, and caddy client certificates.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Certificate validity window in days (default: 90).",
    )
    parser.add_argument(
        "--client-san",
        default="caddy.internal.nce",
        help="DNS SAN assigned to Caddy's internal client certificate.",
    )
    parser.add_argument(
        "--check-expiry-days",
        type=int,
        default=None,
        help="Verify ca.crt, admin.crt, and caddy.crt exist and remain valid for >= N days.",
    )
    args = parser.parse_args(argv)
    if args.check_expiry_days is not None:
        ok, issues = check_internal_mtls_expiry(args.out_dir, min_days=args.check_expiry_days)
        if ok:
            print(
                f"OK: internal mTLS certificates in {args.out_dir} remain valid for >= {args.check_expiry_days} days."
            )
            return 0
        for issue in issues:
            print(f"ERROR: {issue}")
        return 1

    paths = generate_internal_mtls_bundle(
        args.out_dir,
        validity_days=args.days,
        client_san=args.client_san,
    )
    for label, p in paths.items():
        print(f"Generated {label}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
