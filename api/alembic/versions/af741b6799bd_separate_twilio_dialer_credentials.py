"""separate Twilio dialer credentials

Revision ID: af741b6799bd
Revises: cd20261003
Create Date: 2026-10-15 20:03:49.104868

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'af741b6799bd'
down_revision: Union[str, None] = 'cd20261003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_twilio_dialer_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_sid", sa.String(length=64), nullable=False),
        sa.Column("auth_token_encrypted", sa.String(), nullable=False),
        sa.Column("api_key_sid", sa.String(length=64), nullable=False),
        sa.Column("api_key_secret_encrypted", sa.String(), nullable=False),
        sa.Column("twiml_app_sid", sa.String(length=64), nullable=False),
        sa.Column("default_caller_id", sa.String(length=32), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_platform_twilio_dialer_credentials_singleton"),
    )
    # Preserve the currently configured dialer without decrypting or exposing
    # either secret. From this point on, managed telephony edits cannot change it.
    op.execute(sa.text("""
        INSERT INTO platform_twilio_dialer_credentials
            (id, account_sid, auth_token_encrypted, api_key_sid,
             api_key_secret_encrypted, twiml_app_sid, default_caller_id,
             last_validated_at, updated_at)
        SELECT 1, account_sid, auth_token_encrypted, dialer_api_key_sid,
               dialer_api_key_secret_encrypted, dialer_twiml_app_sid,
               dialer_default_caller_id, last_validated_at, now()
        FROM platform_twilio_credentials
        WHERE is_active = true
          AND dialer_api_key_sid IS NOT NULL
          AND dialer_api_key_secret_encrypted IS NOT NULL
          AND dialer_twiml_app_sid IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        UPDATE platform_twilio_credentials AS account
        SET dialer_api_key_sid = dialer.api_key_sid,
            dialer_api_key_secret_encrypted = dialer.api_key_secret_encrypted,
            dialer_twiml_app_sid = dialer.twiml_app_sid,
            dialer_default_caller_id = dialer.default_caller_id
        FROM platform_twilio_dialer_credentials AS dialer
        WHERE account.is_active = true AND account.account_sid = dialer.account_sid AND dialer.id = 1
    """))
    op.drop_table("platform_twilio_dialer_credentials")
