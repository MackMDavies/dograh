"""
Symmetric encryption for platform-level secrets stored at rest.

The Fernet key is derived from OSS_JWT_SECRET — already required in every
deployment (see docker-compose.yaml) — so storing encrypted secrets introduces
no new required env var. Rotating OSS_JWT_SECRET invalidates previously
encrypted blobs; re-enter affected secrets after a rotation.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    secret = os.environ.get("OSS_JWT_SECRET", "change-me-in-production")
    # Derive a stable 32-byte urlsafe-base64 key from the app secret.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* into a urlsafe token string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """
    Decrypt a token produced by :func:`encrypt_secret`.

    Raises ``cryptography.fernet.InvalidToken`` if the blob was tampered with or
    the derived key no longer matches (e.g. OSS_JWT_SECRET was rotated).
    """
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


__all__ = ["encrypt_secret", "decrypt_secret", "InvalidToken"]
