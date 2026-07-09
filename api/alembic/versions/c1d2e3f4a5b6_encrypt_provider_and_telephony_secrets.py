"""encrypt provider and telephony secrets at rest

Encrypts the two plaintext credential columns exposed by the 2026-06 DB breach:
  * org_provider_connections.api_key      (already TEXT — backfill only)
  * telephony_configurations.credentials  (JSON -> TEXT, then backfill)

The model columns now use EncryptedString / EncryptedJSON, so every future
write ciphertexts automatically; this migration re-encrypts existing plaintext
rows. Idempotent: rows already encrypted are skipped (see _is_encrypted). Uses
api.services.crypto, keyed off OSS_JWT_SECRET (present in the api container env
at migration time).

Revision ID: c1d2e3f4a5b6
Revises: f7e8d9c0b1a2
Create Date: 2026-07-09
"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from api.services.crypto import InvalidToken, decrypt_secret, encrypt_secret

# revision identifiers, used by Alembic.
revision = "c1d2e3f4a5b6"
down_revision = "f7e8d9c0b1a2"
branch_labels = None
depends_on = None


def _is_encrypted(value) -> bool:
    """True if value is None or already a valid Fernet token."""
    if value is None:
        return True
    try:
        decrypt_secret(value)
        return True
    except InvalidToken:
        return False


def upgrade() -> None:
    conn = op.get_bind()

    # credentials must become TEXT to hold a Fernet token (api_key is already TEXT).
    op.alter_column(
        "telephony_configurations",
        "credentials",
        type_=sa.Text(),
        postgresql_using="credentials::text",
        existing_nullable=False,
    )

    # Backfill: encrypt existing plaintext api_key rows.
    for rid, val in conn.execute(
        sa.text(
            "SELECT id, api_key FROM org_provider_connections WHERE api_key IS NOT NULL"
        )
    ).fetchall():
        if not _is_encrypted(val):
            conn.execute(
                sa.text("UPDATE org_provider_connections SET api_key = :v WHERE id = :id"),
                {"v": encrypt_secret(val), "id": rid},
            )

    # Backfill: encrypt existing plaintext credentials rows (now TEXT holding JSON).
    for rid, val in conn.execute(
        sa.text(
            "SELECT id, credentials FROM telephony_configurations WHERE credentials IS NOT NULL"
        )
    ).fetchall():
        if not _is_encrypted(val):
            payload = json.loads(val) if isinstance(val, str) else val
            conn.execute(
                sa.text(
                    "UPDATE telephony_configurations SET credentials = :v WHERE id = :id"
                ),
                {"v": encrypt_secret(json.dumps(payload)), "id": rid},
            )


def downgrade() -> None:
    conn = op.get_bind()

    # Decrypt api_key rows back to plaintext (skip any already-plaintext).
    for rid, val in conn.execute(
        sa.text(
            "SELECT id, api_key FROM org_provider_connections WHERE api_key IS NOT NULL"
        )
    ).fetchall():
        try:
            plain = decrypt_secret(val)
        except InvalidToken:
            continue
        conn.execute(
            sa.text("UPDATE org_provider_connections SET api_key = :v WHERE id = :id"),
            {"v": plain, "id": rid},
        )

    # Decrypt credentials rows back to plaintext JSON text, then restore JSON type.
    for rid, val in conn.execute(
        sa.text(
            "SELECT id, credentials FROM telephony_configurations WHERE credentials IS NOT NULL"
        )
    ).fetchall():
        try:
            plain = decrypt_secret(val)
        except InvalidToken:
            continue
        conn.execute(
            sa.text("UPDATE telephony_configurations SET credentials = :v WHERE id = :id"),
            {"v": plain, "id": rid},
        )

    op.alter_column(
        "telephony_configurations",
        "credentials",
        type_=postgresql.JSON(),
        postgresql_using="credentials::json",
        existing_nullable=False,
    )
