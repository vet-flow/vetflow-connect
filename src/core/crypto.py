"""RSA signing and plugin content hashing helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

# cryptography importowane LENIWO (w funkcjach), nie na poziomie modułu. Tryb dev
# (localhost/gruzalab/test/vet-flow-demo) NIE weryfikuje podpisów → nie woła
# sign/verify/generate_keypair, więc cryptography nie ładuje się na demo. Omija to
# ból PyInstaller z natywnym bindingiem Rust (_rust) dla .exe na demo; cryptography
# wchodzi dopiero przy realnej weryfikacji (prod).

SIGNATURE_EXCLUDED_FILES = {"manifest.json", "signature.sig", "__pycache__"}


def generate_keypair() -> tuple[bytes, bytes]:
    """Return a new RSA 2048 private/public keypair in PEM format."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def sign(data: bytes, private_key: bytes) -> bytes:
    """Sign bytes with an RSA private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key, password=None)
    return key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def verify(data: bytes, signature: bytes, public_key: bytes) -> bool:
    """Verify an RSA PKCS1v15 SHA256 signature."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_public_key(public_key)
    try:
        key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


def hash_plugin_files(plugin_dir: Path) -> str:
    """Hash plugin files deterministically, excluding signature metadata."""
    digest = hashlib.sha256()
    for path in sorted(_iter_plugin_files(plugin_dir), key=lambda item: item.as_posix()):
        relative = path.relative_to(plugin_dir).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _iter_plugin_files(plugin_dir: Path):
    for path in plugin_dir.rglob("*"):
        if path.is_dir():
            if path.name == "__pycache__":
                continue
            continue
        if any(part == "__pycache__" for part in path.parts):
            continue
        if path.name in SIGNATURE_EXCLUDED_FILES:
            continue
        yield path
