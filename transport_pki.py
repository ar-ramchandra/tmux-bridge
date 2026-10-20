"""Private transport CA and TLS certificates; no material is uploaded to Cloudflare."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as file:
        path.chmod(0o600)
        file.write(content)


def key_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def init_ca(directory):
    directory = Path(directory)
    key_path, cert_path = directory/'ca.key', directory/'ca.crt'
    if key_path.exists() or cert_path.exists():
        if not (key_path.exists() and cert_path.exists()):
            raise ValueError('Incomplete CA; refusing to replace it')
        return
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Terminal transport private CA')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    write_private(key_path, key_bytes(key))
    write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))


def issue(directory, name, destination, *, server=False, days=30):
    directory, destination = Path(directory), Path(destination)
    ca_key = serialization.load_pem_private_key((directory/'ca.key').read_bytes(), password=None)
    ca = x509.load_pem_x509_certificate((directory/'ca.crt').read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    start = now-timedelta(minutes=5) if days > 0 else now-timedelta(days=2)
    end = now+timedelta(days=days) if days > 0 else now-timedelta(days=1)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    if server:
        cert = cert.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    cert = cert.sign(ca_key, hashes.SHA256())
    write_private(destination/'tls.key', key_bytes(key))
    write_private(destination/'tls.crt', cert.public_bytes(serialization.Encoding.PEM))
    write_private(destination/'ca.crt', ca.public_bytes(serialization.Encoding.PEM))
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
