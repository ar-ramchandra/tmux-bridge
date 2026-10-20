"""
Certificate authority and authentication for tmux-bridge.

- CA keypair generation (gateway init)
- Server cert signing (enrollment)
- Auth proof verification (server connects to gateway)
- Cert revocation
- Enrollment token management
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CONFIG_DIR = Path(os.environ.get("TMUX_BRIDGE_CONFIG_DIR", Path.home() / ".config/tmux-bridge")).expanduser()
CA_KEY_FILE = CONFIG_DIR / "ca.key"
CA_CERT_FILE = CONFIG_DIR / "ca.crt"
SERVERS_DIR = CONFIG_DIR / "servers"
TOKENS_DIR = CONFIG_DIR / "tokens"
REVOKED_FILE = CONFIG_DIR / "revoked.json"


def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SERVERS_DIR.mkdir(exist_ok=True)
    TOKENS_DIR.mkdir(exist_ok=True)


def init_ca() -> tuple[bytes, bytes]:
    """Generate CA keypair and self-signed cert. Returns (ca_key_pem, ca_cert_pem)."""
    ensure_config_dir()

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "tmux-bridge CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "tmux-bridge"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    CA_KEY_FILE.write_bytes(key_pem)
    CA_KEY_FILE.chmod(0o600)
    CA_CERT_FILE.write_bytes(cert_pem)

    return key_pem, cert_pem


def load_ca() -> tuple:
    """Load CA private key and cert. Returns (private_key, certificate)."""
    ca_key = serialization.load_pem_private_key(CA_KEY_FILE.read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate(CA_CERT_FILE.read_bytes())
    return ca_key, ca_cert


def ca_exists() -> bool:
    return CA_KEY_FILE.exists() and CA_CERT_FILE.exists()


def issue_server_cert(server_name: str) -> tuple[bytes, bytes, bytes]:
    """Sign a new cert for a server. Returns (server_key_pem, server_cert_pem, ca_cert_pem)."""
    validate_server_name(server_name)
    ca_key, ca_cert = load_ca()

    server_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, server_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "tmux-bridge"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(server_name)]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    key_pem = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    ca_cert_pem = CA_CERT_FILE.read_bytes()

    # Store a copy on gateway side for reference
    server_dir = SERVERS_DIR / server_name
    server_dir.mkdir(exist_ok=True)
    (server_dir / "cert.pem").write_bytes(cert_pem)
    (server_dir / "meta.json").write_text(json.dumps({
        "name": server_name,
        "issued_at": time.time(),
        "serial": str(cert.serial_number),
    }, indent=2))

    return key_pem, cert_pem, ca_cert_pem


def verify_auth_proof(cert_pem: bytes, signature_b64: str, timestamp: str) -> str:
    """
    Verify a server's auth proof: cert signed by our CA, not revoked,
    signature of timestamp is valid, timestamp is fresh (<30s).
    Returns the server name (CN) on success, raises ValueError on failure.
    """
    # Parse cert
    cert = x509.load_pem_x509_certificate(cert_pem)

    # Check cert is signed by our CA
    ca_key, ca_cert = load_ca()
    try:
        ca_cert.public_key().verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            ec.ECDSA(cert.signature_hash_algorithm),
        )
    except Exception:
        raise ValueError("Certificate not signed by this CA")

    # Check not expired
    now = datetime.now(timezone.utc)
    if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
        raise ValueError("Certificate expired or not yet valid")

    # Extract server name
    server_name = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

    # Check not revoked
    if is_revoked(server_name):
        raise ValueError(f"Certificate for '{server_name}' has been revoked")

    # Verify signature of timestamp
    signature = base64.b64decode(signature_b64)
    try:
        cert.public_key().verify(
            signature,
            timestamp.encode(),
            ec.ECDSA(hashes.SHA256()),
        )
    except Exception:
        raise ValueError("Invalid signature")

    # Check timestamp freshness
    try:
        ts = int(timestamp)
    except ValueError:
        raise ValueError("Invalid timestamp format")
    if abs(time.time() - ts) > 30:
        raise ValueError("Timestamp too old or too far in the future")

    return server_name


def create_auth_proof(key_pem: bytes, cert_pem: bytes) -> dict:
    """Create an auth proof message for a server connecting to gateway."""
    key = serialization.load_pem_private_key(key_pem, password=None)
    timestamp = str(int(time.time()))
    signature = key.sign(timestamp.encode(), ec.ECDSA(hashes.SHA256()))

    return {
        "type": "auth",
        "cert": cert_pem.decode(),
        "signature": base64.b64encode(signature).decode(),
        "timestamp": timestamp,
    }


# ── Revocation ──

def _load_revoked() -> set[str]:
    try:
        return set(json.loads(REVOKED_FILE.read_text()))
    except Exception:
        return set()


def _save_revoked(revoked: set[str]):
    ensure_config_dir()
    REVOKED_FILE.write_text(json.dumps(sorted(revoked), indent=2))


def revoke_server(server_name: str):
    """Revoke a server's certificate."""
    revoked = _load_revoked()
    revoked.add(server_name)
    _save_revoked(revoked)
    # Clean up server directory
    server_dir = SERVERS_DIR / server_name
    if server_dir.exists():
        for f in server_dir.iterdir():
            f.unlink()
        server_dir.rmdir()


def is_revoked(server_name: str) -> bool:
    return server_name in _load_revoked()


def list_registered_servers() -> list[dict]:
    """List all servers that have been issued certs."""
    servers = []
    if not SERVERS_DIR.exists():
        return servers
    revoked = _load_revoked()
    for d in sorted(SERVERS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_file = d / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            meta["revoked"] = meta["name"] in revoked
            servers.append(meta)
    return servers


# ── Enrollment tokens ──

def create_enrollment_token(server_name: str, ttl: int = 3600) -> str:
    """Create a one-time enrollment token for a server. Returns token string."""
    validate_server_name(server_name)
    ensure_config_dir()
    token = secrets.token_urlsafe(32)
    token_file = TOKENS_DIR / f"{token}.json"
    token_file.write_text(json.dumps({
        "server_name": server_name,
        "created_at": time.time(),
        "expires_at": time.time() + ttl,
    }, indent=2))
    token_file.chmod(0o600)
    return token


def consume_enrollment_token(token: str) -> str | None:
    """
    Consume an enrollment token. Returns server_name if valid, None if
    expired/invalid/already-used.
    """
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    token_file = TOKENS_DIR / f"{token}.json"
    if not token_file.exists():
        return None
    try:
        data = json.loads(token_file.read_text())
    except Exception:
        return None

    # Check expiry
    if time.time() > data.get("expires_at", 0):
        token_file.unlink(missing_ok=True)
        return None

    # Consume (one-time use)
    token_file.unlink()
    return data.get("server_name")


def validate_server_name(name: str):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", name):
        raise ValueError("Server names must use 1–63 letters, numbers, hyphens or underscores, starting with a letter or number.")


def cleanup_expired_tokens():
    """Remove expired tokens."""
    if not TOKENS_DIR.exists():
        return
    now = time.time()
    for f in TOKENS_DIR.iterdir():
        if not f.name.endswith(".json"):
            continue
        try:
            data = json.loads(f.read_text())
            if now > data.get("expires_at", 0):
                f.unlink()
        except Exception:
            pass
