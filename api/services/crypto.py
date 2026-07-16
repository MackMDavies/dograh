"""
Symmetric encryption for platform-level secrets stored at rest.

The Fernet key is derived from OSS_JWT_SECRET — already required in every
deployment (see docker-compose.yaml) — so storing encrypted secrets introduces
no new required env var. Rotating OSS_JWT_SECRET invalidates previously
encrypted blobs; re-enter affected secrets after a rotation.
"""
import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator


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


class EncryptedString(TypeDecorator):
    """Transparently Fernet-encrypt a string column at rest.

    Read tolerates legacy plaintext (returns it unchanged) so a column can be
    migrated in place: existing rows keep working until backfilled, and every
    write stores ciphertext.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return decrypt_secret(value)
        except InvalidToken:
            return value  # legacy plaintext during the migration window


class EncryptedJSON(TypeDecorator):
    """Fernet-encrypt a JSON/dict column at rest (stored as ciphertext text).

    Read tolerates legacy plaintext JSON so the column can be migrated in place.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_secret(json.dumps(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return json.loads(decrypt_secret(value))
        except InvalidToken:
            return json.loads(value)  # legacy plaintext JSON


__all__ = [
    "encrypt_secret",
    "decrypt_secret",
    "InvalidToken",
    "EncryptedString",
    "EncryptedJSON",
]
